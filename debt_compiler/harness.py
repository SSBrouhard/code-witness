"""Trusted local Python execution in fresh processes. This is not a sandbox."""
import ast
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from typing import Literal, Protocol

from .invariants import check
from .manifest import relative, resolve_source_root


class DiffClassifier(Protocol):
    """Future classifier interface only. No implementation or paid calls."""
    def classify(self, legacy: dict, modern: dict) -> Literal["noise", "semantic_break", "invariant_violation"]: ...


def worker(module, case):
    stdout, stderr, sleeps = io.StringIO(), io.StringIO(), []
    observation = {"result": None, "exception": None}
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        sys.path.insert(0, str(Path(module).parent))
        spec = importlib.util.spec_from_file_location("slice_under_test", module)
        target = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(target)
        if not callable(getattr(target, "run", None)):
            raise RuntimeError("Selected batch.py must define run(case, sleep=...)")
        try:
            observation["result"] = target.run(case, sleep=lambda seconds: sleeps.append(seconds))
        except Exception as exc:
            observation["exception"] = {"type": type(exc).__name__, "message": str(exc)}
    observation.update(stdout=stdout.getvalue(), stderr=stderr.getvalue(), sleeps=sleeps)
    return observation


def run_case(module, case, timeout=5):
    try:
        completed = subprocess.run([sys.executable, "-m", "debt_compiler.harness", "--worker", str(module)],
                                   input=json.dumps(case), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Harness timed out after {timeout}s: {module}") from exc
    if completed.returncode:
        raise RuntimeError(f"Harness worker exited {completed.returncode}: {module}; {completed.stderr[-500:]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Harness worker did not return JSON: {module}") from exc


def has_entrypoint(path):
    """Recognize the synchronous top-level protocol without importing foreign code."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            args = node.args
            positional = args.posonlyargs + args.args
            keyword_names = {arg.arg for arg in args.args + args.kwonlyargs}
            return bool(positional and positional[0].arg == "case" and "sleep" in keyword_names)
    return False


def execute(files, rules, source_root):
    # Exact convention only: a selected batch.py plus ../fixtures/cases.json.
    entries = [(p, p.parent.parent / "fixtures" / "cases.json") for p in files if p.name == "batch.py"]
    entries = [(p, f) for p, f in entries if f.is_file()]
    if not entries:
        return {"status": "not-run", "reason": "No selected batch.py with sibling fixtures/cases.json", "rows": []}
    if len(entries) != 1:
        raise ValueError("Select one runnable batch.py slice at a time")
    legacy, fixture_file = entries[0]
    if not has_entrypoint(legacy):
        return {"status": "not-run", "reason": "Selected batch.py has no top-level run(case, sleep=...) convention", "rows": []}
    modern = legacy.parent.parent / "modern" / "batch.py"
    if modern == legacy or not modern.is_file():
        modern = None
    cases = json.loads(fixture_file.read_text())
    if not isinstance(cases, list) or not cases:
        raise ValueError("fixtures/cases.json must be a nonempty list")
    names = set()
    rows = []
    for fixture in cases:
        if not isinstance(fixture, dict) or not isinstance(fixture.get("name"), str) or not isinstance(fixture.get("input"), dict):
            raise ValueError("Each fixture requires a string name and object input")
        if fixture["name"] in names:
            raise ValueError("Fixture names must be unique")
        names.add(fixture["name"])
        left = run_case(legacy, fixture["input"])
        right = run_case(modern, fixture["input"]) if modern else None
        checks = {rule["id"]: {"legacy": check(rule, fixture["input"], left),
                               "modern": check(rule, fixture["input"], right) if right is not None else "not-run"} for rule in rules}
        rows.append({**fixture, "legacy": left, "modern": right,
                     "equal": left == right if modern else None, "checks": checks})
    return {"status": "executed", "legacy": relative(legacy, source_root), "modern": relative(modern, source_root) if modern else None, "rows": rows}


def report(result):
    lines = ["# Dual run", "", "Fresh subprocess per fixture and implementation, five-second timeout. Trusted code only: no sandbox. Compare exact JSON returns, exception type/message, stdout, stderr, and requested sleeps. Sleep is injected as a recorder; wall-clock, network, filesystem and external-service effects are not verified. Classifier is an interface stub and is not called.", ""]
    if result["status"] == "not-run":
        return "\n".join(lines + [f"NOT RUN: {result['reason']}", ""])
    lines += [f"Legacy: `{result['legacy']}`", f"Modern: `{result['modern'] or 'absent - comparison not run'}`", ""]
    for row in result["rows"]:
        verdict = "MATCH" if row["equal"] else "MISMATCH" if row["equal"] is False else "LEGACY ONLY"
        lines += [f"## {row['name']}: {verdict}", "", "```json", json.dumps({k: row[k] for k in ("legacy", "modern", "checks")}, indent=2), "```", ""]
    return "\n".join(lines)


def write_tests(out, result):
    directory = out / "tests"
    directory.mkdir(exist_ok=True)
    (directory / "observations.json").write_text(json.dumps(result, indent=2) + "\n")
    (directory / "test_characterization.py").write_text('''"""Generated from observed legacy outputs, not hand-authored expected behavior."""
import argparse
import json
from pathlib import Path
import unittest
from debt_compiler.harness import run_case
from debt_compiler.manifest import resolve_source_root

PACK = Path(__file__).resolve().parents[1]
DATA = json.loads(Path(__file__).with_name("observations.json").read_text())
MANIFEST = json.loads((PACK / "manifest.json").read_text())
SOURCE_ROOT = resolve_source_root(PACK, MANIFEST)

class Characterization(unittest.TestCase):
    def test_observed_contract(self):
        if DATA["status"] != "executed":
            self.skipTest("NOT RUN: " + DATA["reason"])
        for row in DATA["rows"]:
            for target in ("legacy", "modern"):
                if DATA.get(target):
                    with self.subTest(fixture=row["name"], target=target):
                        self.assertEqual(run_case(SOURCE_ROOT / DATA[target], row["input"]), row["legacy"])

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", help="Relocated repository/source root")
    args, remaining = parser.parse_known_args()
    SOURCE_ROOT = resolve_source_root(PACK, MANIFEST, args.source_root)
    unittest.main(argv=[__file__, *remaining])
''')


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("Internal worker: --worker MODULE")
    print(json.dumps(worker(sys.argv[2], json.load(sys.stdin)), allow_nan=False))
