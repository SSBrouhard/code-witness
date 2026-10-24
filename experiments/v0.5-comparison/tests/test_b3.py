"""Paired, synthetic G2 canaries. Never reads the stopped experiment's evidence."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from test_score import call, commit, score, sealed


class PairedKnockout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def pair(self, source, adapter_body, test_body, adapter_import="from canary import core", cases=None, extras=None):
        entries = []
        for name in ("witness", "pytest"):
            tree = self.tmp / name
            (tree / "canary").mkdir(parents=True)
            (tree / "canary/__init__.py").write_text("")
            (tree / "canary/core.py").write_text(source)
            for filename, content in (extras or {}).items():
                (tree / filename).write_text(content)
            commit(tree)
            (tree / ".git/info/exclude").write_text("_witness/\n_characterization/\n")
            if name == "witness":
                pack = tree / "_witness/pack"
                (pack / "tests").mkdir(parents=True)
                adapter = tree / "_witness/adapter.py"
                adapter.write_text("import sys\nfrom pathlib import Path\n"
                                   "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
                                   + adapter_import + "\n\ndef run(case, sleep):\n" + adapter_body)
                (pack / "manifest.json").write_text(json.dumps({"default_source_root": "../..",
                                                               "source_roots": [{"path": "."}]}))
                (pack / "tests/observations.json").write_text(json.dumps({
                    "legacy": "_witness/adapter.py", "rows": [
                        {"name": label, "input": case, "legacy": score.run_case(adapter, case)}
                        for label, case in (cases or [("case", {})])]}))
                entries.append({"name": name, "kind": name, "tree": name})
            else:
                tests = tree / "_characterization/tests"
                tests.mkdir(parents=True)
                (tree / "_characterization/pytest.ini").write_text("[pytest]\n")
                (tests / "test_probe.py").write_text(test_body)
                entries.append({"name": name, "kind": name, "tree": name, "deliverable": "_characterization",
                                "argv": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                         "-c", "_characterization/pytest.ini", "_characterization/tests"],
                                "env": {"PYTHONPATH": "."}, "timeout": 10})
        path = self.tmp / "suites.json"
        path.write_text(json.dumps(sealed(self.tmp, entries)))
        return path

    def knockout(self, suites, name):
        result = call("knockout", "--suites", suites, "--scope", "canary/core.py", "--suite", name,
                      "--methodology", "b3")
        score.require_clean(self.tmp / name)
        score.snapshot(score.load_suites(suites))
        self.assertEqual(result["methodology"], "B3-supplemental-post-hoc")
        return result

    def check_pair(self, suites, status, kind, reached=0, dependency=False):
        for name in ("witness", "pytest"):
            with self.subTest(kind=name):
                result = self.knockout(suites, name)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["counts"], {k: int(k == kind) for k in
                                                   ("changed", "unchanged", "blocked", "errored")})
                self.assertEqual(result["reached_target"], reached)
                self.assertEqual(bool(result["dependency_evidence"]), dependency)

    def test_real_target_execution_has_per_item_evidence(self):
        suites = self.pair("def value():\n    return 7\n", "    return core.value()\n",
                           "from canary import core\n\ndef test_calls_target():\n    assert core.value() == 7\n")
        self.check_pair(suites, "pass", "changed", reached=1)

    def test_probe_does_not_shadow_target_modules_with_scoring_tools(self):
        suites = self.pair("import score\n\ndef value():\n    return score.VALUE\n",
                           "    return core.value()\n",
                           "from canary import core\n\ndef test_calls_target():\n    assert core.value() == 7\n",
                           extras={"score.py": "VALUE = 7\n"})
        self.check_pair(suites, "pass", "changed", reached=1)

    def test_import_only_without_initialization_has_no_execution_credit(self):
        suites = self.pair("def value():\n    return 7\n", "    return 7\n",
                           "from canary import core\n\ndef test_import_only():\n    assert core is not None\n")
        self.check_pair(suites, "fail", "unchanged")

    def test_execution_evidence_cannot_leak_to_another_item(self):
        suites = self.pair("def value():\n    return 7\n",
                           '    if case["call"]:\n        return core.value()\n'
                           '    return "code-witness knockout" not in core.value.__code__.co_consts\n',
                           'from canary import core\n\ndef test_a_call():\n    assert core.value() == 7\n'
                           '\ndef test_b_unrelated():\n    assert "code-witness knockout" not in core.value.__code__.co_consts\n',
                           cases=[("call", {"call": True}), ("unrelated", {"call": False})])
        for name in ("witness", "pytest"):
            result = self.knockout(suites, name)
            self.assertEqual(result["counts"], {"changed": 1, "unchanged": 0, "blocked": 0, "errored": 1})
            self.assertEqual(result["reached_target"], 1)
            self.assertEqual({e["unit"] for e in result["runtime_evidence"]}, set(result["cases"]["changed"]))

    def test_parameterized_class_items_keep_their_own_node_ids(self):
        suites = self.pair("def value():\n    return 7\n", "    return core.value()\n",
                           'import pytest\nfrom canary import core\n\nclass TestCalls:\n'
                           '    @pytest.mark.parametrize("n", [1, 2])\n'
                           '    def test_call(self, n):\n        assert core.value() == 7\n',
                           cases=[("one", {}), ("two", {})])
        for name in ("witness", "pytest"):
            result = self.knockout(suites, name)
            self.assertEqual(result["counts"]["changed"], 2)
            self.assertEqual(result["reached_target"], 2)
            self.assertEqual(result["status"], "pass")

    def test_module_failure_blocks_every_affected_unit_without_positive_credit(self):
        suites = self.pair("def value():\n    return 7\n\nINITIAL = value()\n", "    return 7\n",
                           "from canary import core\n\ndef test_one():\n    assert core is not None\n"
                           "\ndef test_two():\n    assert core is not None\n", cases=[("one", {}), ("two", {})])
        for name in ("witness", "pytest"):
            result = self.knockout(suites, name)
            self.assertEqual(result["units"], 2)
            self.assertEqual(result["counts"], {"changed": 0, "unchanged": 0, "blocked": 2, "errored": 0})
            self.assertEqual(result["status"], "insufficient-evidence")

    def test_lazy_import_failure_is_still_only_import_dependency(self):
        suites = self.pair("def value():\n    return 7\n\nINITIAL = value()\n",
                           "    from canary import core\n    return 7\n",
                           "def test_lazy_import_only():\n    from canary import core\n    assert core is not None\n",
                           adapter_import="")
        self.check_pair(suites, "insufficient-evidence", "blocked", dependency=True)

    def test_caught_import_failure_with_no_outcome_change_is_only_dependency_evidence(self):
        body = "    try:\n        from canary import core\n    except Exception:\n        pass\n"
        suites = self.pair("def value():\n    return 7\n\nINITIAL = value()\n", body + "    return 7\n",
                           "def test_catches_import():\n" + body, adapter_import="")
        self.check_pair(suites, "fail", "unchanged", dependency=True)

    def test_per_item_setup_target_execution_is_not_confused_with_collection(self):
        suites = self.pair("def value():\n    return 7\n", "    return core.value()\n",
                           "import pytest\nfrom canary import core\n\n@pytest.fixture\ndef value():\n    return core.value()\n"
                           "\ndef test_value(value):\n    assert value == 7\n")
        self.check_pair(suites, "pass", "changed", reached=1)

    def test_shared_setup_failure_is_not_recredited_to_later_items(self):
        suites = self.pair("def value():\n    return 7\n", "    return core.value()\n",
                           "import pytest\nfrom canary import core\n\n@pytest.fixture(scope='session')\n"
                           "def value():\n    return core.value()\n\ndef test_one(value):\n    assert value == 7\n"
                           "\ndef test_two(value):\n    assert value == 7\n")
        result = self.knockout(suites, "pytest")
        self.assertEqual(result["counts"], {"changed": 1, "unchanged": 0, "blocked": 0, "errored": 1})
        self.assertEqual(result["reached_target"], 1)
        self.assertEqual(result["status"], "insufficient-evidence")

    def test_explicit_collection_time_target_call_is_not_item_execution(self):
        suites = self.pair("def value():\n    return 7\n", "    return 7\n",
                           "from canary import core\nINITIAL = core.value()\n\ndef test_import_only():\n    assert INITIAL == 7\n",
                           adapter_import="from canary import core\nINITIAL = core.value()")
        self.check_pair(suites, "insufficient-evidence", "blocked", dependency=True)

    def test_unrelated_pre_item_exception_with_marker_text_is_not_attributed(self):
        guard = ('if "code-witness knockout" in core.value.__code__.co_consts:\n'
                 '    raise RuntimeError("code-witness knockout")\n')
        suites = self.pair("def value():\n    return 7\n", "    return 7\n",
                           "from canary import core\n" + guard + "\ndef test_import_only():\n    assert core is not None\n",
                           adapter_import="from canary import core\n" + guard)
        self.check_pair(suites, "insufficient-evidence", "errored")

    def test_other_exception_from_target_scope_during_collection_is_not_a_knockout(self):
        source = ('def value():\n    return 7\n\n'
                  'if "code-witness knockout" in value.__code__.co_consts:\n'
                  '    raise ValueError("code-witness knockout")\n')
        suites = self.pair(source, "    return 7\n",
                           "from canary import core\n\ndef test_import_only():\n    assert core is not None\n")
        self.check_pair(suites, "insufficient-evidence", "errored")

    def test_missing_dependency_during_collection_is_not_a_knockout(self):
        source = ('def value():\n    return 7\n\n'
                  'if "code-witness knockout" in value.__code__.co_consts:\n'
                  '    raise ModuleNotFoundError("missing unrelated dependency")\n')
        suites = self.pair(source, "    return 7\n",
                           "from canary import core\n\ndef test_import_only():\n    assert core is not None\n")
        self.check_pair(suites, "insufficient-evidence", "errored")

    def test_unrelated_hard_worker_exit_never_receives_credit(self):
        guard = '    if "code-witness knockout" in core.value.__code__.co_consts:\n        os._exit(19)\n'
        suites = self.pair("def value():\n    return 7\n", guard + "    return 7\n",
                           "import os\nfrom canary import core\n\ndef test_worker_exit():\n" + guard,
                           adapter_import="import os\nfrom canary import core")
        self.check_pair(suites, "insufficient-evidence", "errored")

    def test_unrelated_runtime_assertion_or_observation_change_is_not_target_execution(self):
        suites = self.pair("def value():\n    return 7\n",
                           '    return "code-witness knockout" not in core.value.__code__.co_consts\n',
                           'from canary import core\n\ndef test_introspection():\n'
                           '    assert "code-witness knockout" not in core.value.__code__.co_consts\n')
        self.check_pair(suites, "insufficient-evidence", "errored")

    def test_caught_target_knockout_with_changed_behavior_counts_in_both_formats(self):
        body = "    try:\n        result = core.value()\n    except Exception:\n        result = 0\n"
        suites = self.pair("def value():\n    return 7\n", body + "    return result\n",
                           "from canary import core\n\ndef test_catches_target():\n" + body + "    assert result == 7\n")
        self.check_pair(suites, "pass", "changed", reached=1)

    def test_execution_without_observed_sensitivity_does_not_pass(self):
        body = "    try:\n        core.value()\n    except Exception:\n        pass\n"
        suites = self.pair("def value():\n    return 7\n", body + "    return 7\n",
                           "from canary import core\n\ndef test_swallows_target():\n" + body)
        self.check_pair(suites, "fail", "unchanged", reached=1)

    def test_same_named_exception_from_adapter_or_test_is_not_a_target_knockout(self):
        body = ('    if "code-witness knockout" in core.value.__code__.co_consts:\n'
                '        raise type("CodeWitnessKnockout", (Exception,), {})("code-witness knockout")\n')
        suites = self.pair("def value():\n    return 7\n", body + "    return 7\n",
                           "from canary import core\n\ndef test_lookalike():\n" + body)
        self.check_pair(suites, "insufficient-evidence", "errored")

    def test_import_time_knockout_never_credits_an_import_only_item(self):
        suites = self.pair("def value():\n    return 7\n\nINITIAL = value()\n", "    return 7\n",
                           "from canary import core\n\ndef test_import_only():\n    assert core is not None\n")
        results = {n: self.knockout(suites, n) for n in ("witness", "pytest")}
        for name, result in results.items():
            self.assertEqual(result["counts"]["changed"], 0, name)
            self.assertEqual(result["status"], "insufficient-evidence", name)
            self.assertEqual(result["counts"]["blocked"], 1, name)
            self.assertEqual(result["reached_target"], 0, name)
            self.assertEqual(len(result["dependency_evidence"]), 1, name)


if __name__ == "__main__":
    unittest.main()
