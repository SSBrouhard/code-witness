"""v0.5 scorer: named suites, one scoring tree per suite, identical mutations across trees.

Deterministic support for docs/v0.5-comparison-protocol.md. Not part of debt-compiler.
No command here runs through a shell; cosmic-ray is used only to enumerate and apply mutants.

Suites are declared in a JSON file. Relative trees resolve against that file's directory; pack,
adapter and cases resolve against the suite's own tree:
  [{"name": "W", "kind": "witness", "tree": "trees/W", "pack": "_witness/pack"},
   {"name": "X", "kind": "pytest", "tree": "trees/X", "timeout": 60, "env": {"PYTHONPATH": "."},
    "argv": ["/abs/python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "_characterization/pytest.ini",
             "--rootdir=_characterization", "--confcutdir=_characterization", "_characterization/tests"]},
   {"name": "upstream", "kind": "pytest", "tree": "trees/upstream", "timeout": 60, "argv": [...]}]
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

# Imported helpers load through normal Python caching; stale .pyc files can hide mutants.
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from debt_compiler.harness import observation_json, run_case
from debt_compiler.manifest import resolve_source_root

COSMIC = str(Path(sys.executable).with_name("cosmic-ray"))
KNOCKOUT_MARKER = "code-witness knockout"
# A class created at raise time: no test that expects a specific built-in exception can catch it by accident.
KNOCKOUT = f'raise type("CodeWitnessKnockout", (Exception,), {{}})("{KNOCKOUT_MARKER}")'
KNOCKOUT_THRESHOLD = 0.8
# pytest --tb=long ends each failure with "path:line: ExceptionType".
FAILURE_ORIGIN = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<type>[A-Za-z_][\w.]*)$")
ASSERTION_TYPES = {"AssertionError", "Failed"}  # Failed: pytest.raises/fail outcomes such as DID NOT RAISE
DEPENDENCY_TYPES = {"ImportError", "ModuleNotFoundError"}
KILL_CLASSES = ("assertion", "target-exception")
ERROR_CLASSES = ("dependency", "collection", "setup")
# Loaded with -p from a private directory; reverses collection order for the reproducibility check.
REVERSE_PLUGIN = "def pytest_collection_modifyitems(session, config, items):\n    items.reverse()\n"


def git(tree, *args):
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=True).stdout


def purge_bytecode(tree):
    tree = Path(tree).resolve()
    for cache in list(tree.rglob("__pycache__")):
        if cache.is_dir() and not cache.is_symlink() and cache.resolve().is_relative_to(tree):
            shutil.rmtree(cache)


def require_clean(tree):
    purge_bytecode(tree)
    status = git(tree, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise SystemExit(f"S5: tree is not clean: {tree}\n{status}")


def restore(tree):
    git(tree, "checkout", "--", ".")
    # Removes files a patch added; deliverable directories are excluded, so git clean (without -x) keeps them.
    git(tree, "clean", "-fdq")
    require_clean(tree)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def load_suites(path):
    """Validated suite declarations with absolute paths. Names are unique; trees are distinct."""
    path = Path(path).resolve()
    suites = json.loads(path.read_text())
    names, trees = [s.get("name") for s in suites], [s.get("tree") for s in suites]
    if not suites or len(set(names)) != len(names) or None in names:
        raise SystemExit("suites: names must be present and unique")
    resolved = []
    for suite in suites:
        tree = (path.parent / suite["tree"]).resolve()
        entry = {"name": suite["name"], "kind": suite.get("kind"), "tree": tree}
        if entry["kind"] == "witness":
            entry["pack"] = tree / suite.get("pack", "_witness/pack")
            entry["adapter"] = tree / suite.get("adapter", "_witness/slice/legacy/batch.py")
            entry["cases"] = tree / suite.get("cases", "_witness/slice/fixtures/cases.json")
        elif entry["kind"] == "pytest":
            argv = suite.get("argv")
            if not argv or argv[1:3] != ["-m", "pytest"]:
                raise SystemExit(f"suites: {suite['name']}: argv must start with PYTHON -m pytest")
            entry.update(argv=list(argv), env=dict(suite.get("env", {})), timeout=float(suite.get("timeout", 300)))
        else:
            raise SystemExit(f"suites: {suite['name']}: kind must be witness or pytest")
        resolved.append(entry)
    if len({s["tree"] for s in resolved}) != len(resolved):
        raise SystemExit("suites: every suite needs its own tree")
    return resolved


def classify(behavioral, timeouts, errors, unclassified=()):
    """Only an observed behavioral difference counts as a kill; uncertain failures go to adjudication."""
    if behavioral:
        return "behavioral"
    if unclassified:
        return "adjudicate"
    if timeouts:
        return "timeout"
    if errors:
        return "error"
    return "survived"


def classify_failure(text, scope):
    """Classify one pytest junit <failure> by its exception type and where it was raised."""
    lines = (text or "").strip().splitlines()
    origin = FAILURE_ORIGIN.match(lines[-1].strip()) if lines else None
    if origin is None:
        return "unclassified"
    kind = origin["type"].rsplit(".", 1)[-1]
    if kind in ASSERTION_TYPES:
        return "assertion"
    if kind in DEPENDENCY_TYPES:
        return "dependency"
    if Path(origin["path"]).as_posix() in scope:
        return "target-exception"
    return "unclassified"


def junit_cases(root, scope):
    """Every test item in a pytest junit report: passed, skipped, or its failure class."""
    cases = {}
    for case in root.iter("testcase"):
        name = f"{case.get('classname')}::{case.get('name')}"
        failure, error = case.find("failure"), case.find("error")
        if failure is not None:
            cases[name] = {"class": classify_failure(failure.text, scope),
                           "knockout": KNOCKOUT_MARKER in (failure.text or "")}
        elif error is not None:
            cases[name] = {"class": "collection" if error.get("message") == "collection failure" else "setup"}
        else:
            cases[name] = {"class": "skipped" if case.find("skipped") is not None else "passed"}
    return cases


def run_pytest(suite, scope, argv=None, env=None, cwd=None):
    """Run a pytest suite with junit output. Returns (exit code or None on timeout, cases, output tail)."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp, "junit.xml")
        try:
            done = subprocess.run([*(argv or suite["argv"]), "--tb=long", f"--junitxml={report}"],
                                  cwd=cwd or suite["tree"], env={**os.environ, **suite["env"], **(env or {})},
                                  capture_output=True, text=True, timeout=suite["timeout"])
        except subprocess.TimeoutExpired:
            return None, {}, ""
        cases = junit_cases(ET.parse(report).getroot(), scope) if report.is_file() else {}
        return done.returncode, cases, (done.stdout + done.stderr)[-500:]


def pytest_suite(suite, scope):
    """junit classes: assertion and target-exception are kills; dependency, collection and setup
    failures are errors; any other failure is unclassified and goes to adjudication."""
    code, cases, tail = run_pytest(suite, scope)
    if code is None:
        return {"class": "timeout", "behavioral": [], "timeouts": [f"suite exceeded {suite['timeout']}s"],
                "errors": [], "unclassified": [], "tests": {}}
    tests = {t: c["class"] for t, c in cases.items() if c["class"] not in ("passed", "skipped")}
    behavioral = sorted(t for t, c in tests.items() if c in KILL_CLASSES)
    unclassified = sorted(t for t, c in tests.items() if c == "unclassified")
    errors = sorted(f"{t}: {c}" for t, c in tests.items() if c in ERROR_CLASSES)
    if code and not tests:
        errors.append(f"exit {code}: {tail}")
    if not code and tests:
        errors.append("exit 0 with recorded failures or errors")
    if not cases and not code:
        errors.append("no tests collected")
    return {"class": classify(behavioral, [], errors, unclassified), "behavioral": behavioral, "timeouts": [],
            "errors": errors, "unclassified": unclassified, "tests": tests, "exit": code, "items": len(cases)}


def witness_suite(suite):
    """Same comparison as the pack's generated test, reported per fixture."""
    pack = suite["pack"]
    data = json.loads((pack / "tests" / "observations.json").read_text())
    root = resolve_source_root(pack, json.loads((pack / "manifest.json").read_text()))
    mismatched, timeouts, errors = [], [], []
    for row in data["rows"]:
        for target in ("legacy", "modern"):
            if not data.get(target):
                continue
            label = f"{row['name']}:{target}"
            try:
                observed = run_case(root / data[target], row["input"])
            except RuntimeError as exc:
                (timeouts if "timed out" in str(exc) else errors).append(f"{label}: {str(exc)[:300]}")
                continue
            if observation_json(observed) != observation_json(row["legacy"]):
                mismatched.append(label)
    return {"class": classify(mismatched, timeouts, errors), "behavioral": mismatched,
            "timeouts": timeouts, "errors": errors, "fixtures": len(data["rows"])}


def run_suites(suites, scope):
    """Run each suite on its own tree; an error result is rerun once before it is final."""
    results = {}
    for suite in suites:
        attempts = []
        for _ in range(2):
            start = time.monotonic()
            purge_bytecode(suite["tree"])
            result = witness_suite(suite) if suite["kind"] == "witness" else pytest_suite(suite, scope)
            result["seconds"] = round(time.monotonic() - start, 3)
            attempts.append(result)
            if result["class"] != "error":
                break
        results[suite["name"]] = {**attempts[-1], "attempts": len(attempts)}
    return results


def identical_diff(suites, label):
    """Every suite tree must hold byte-identical changed source; otherwise no result can count."""
    diffs = {s["name"]: git(s["tree"], "diff") for s in suites}
    if len({digest(d) for d in diffs.values()}) != 1:
        raise SystemExit(f"S5: suite trees differ for {label}: " +
                         json.dumps({name: digest(d) for name, d in diffs.items()}))
    return next(iter(diffs.values()))


def validity(args, tree, diff):
    if not diff:
        return "not-applied"
    for name in git(tree, "diff", "--name-only").split():
        try:
            compile((tree / name).read_bytes(), name, "exec")
        except SyntaxError:
            return "non-compiling"
    if args.import_module:
        code = "import importlib, sys; sys.path.insert(0, sys.argv[1]); [importlib.import_module(m) for m in sys.argv[2:]]"
        try:
            done = subprocess.run([sys.executable, "-c", code, str(tree / args.import_root), *args.import_module],
                                  cwd=tree, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            return "import-failure"
        if done.returncode:
            return "import-failure"
    return "valid"


def enumerate_mutants(args):
    tree = Path(args.tree).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp, "enumerate.toml")
        # test-command is required by cosmic-ray's config but never executed: exec is not called.
        config.write_text("[cosmic-ray]\nmodule-path = " + json.dumps([str(tree / f) for f in args.scope]) +
                          '\ntimeout = 1.0\nexcluded-modules = []\ntest-command = "false"\n'
                          '[cosmic-ray.distributor]\nname = "local"\n')
        session = Path(tmp, "session.sqlite")
        subprocess.run([COSMIC, "init", str(config), str(session)], check=True, capture_output=True)
        dump = subprocess.run([COSMIC, "dump", str(session)], check=True, capture_output=True, text=True).stdout
    keys = []
    for line in dump.splitlines():
        (mutation,) = json.loads(line)[0]["mutations"]
        path = Path(mutation["module_path"]).relative_to(tree).as_posix()
        keys.append({"id": f"{path}:{mutation['operator_name']}:{mutation['occurrence']}", "path": path,
                     "operator": mutation["operator_name"], "occurrence": mutation["occurrence"],
                     "line": mutation["start_pos"][0], "definition": mutation.get("definition_name")})
    keys.sort(key=lambda k: (k["path"], k["operator"], k["occurrence"]))
    selected = keys if len(keys) <= args.cap else sorted(
        random.Random(args.seed).sample(keys, args.cap), key=lambda k: (k["path"], k["operator"], k["occurrence"]))
    Path(args.out).write_text(json.dumps({"scope": args.scope, "total": len(keys), "seed": args.seed,
                                          "cap": args.cap, "selected": selected}, indent=2) + "\n")
    print(json.dumps({"total": len(keys), "selected": len(selected)}))


def run_mutants(args):
    suites = load_suites(args.suites)
    mutants = json.loads(Path(args.mutants).read_text())["selected"][args.start:args.stop]
    with open(args.out, "a") as out:
        for mutant in mutants:
            for suite in suites:
                require_clean(suite["tree"])
            try:
                applied = [subprocess.run([COSMIC, "apply", str(s["tree"] / mutant["path"]), mutant["operator"],
                                           str(mutant["occurrence"])], capture_output=True, text=True).returncode
                           for s in suites]
                diff = identical_diff(suites, mutant["id"])
                if len(set(applied)) != 1:
                    raise SystemExit(f"S5: cosmic-ray apply disagreed across suite trees: {mutant['id']}")
                diff = diff if not applied[0] else ""
                record = {**mutant, "diff_sha256": digest(diff), "diff": diff,
                          "validity": validity(args, suites[0]["tree"], diff)}
                if record["validity"] == "valid":
                    record["suites"] = run_suites(suites, set(args.scope))
                    # Every suite must have observed byte-identical mutated source throughout.
                    if digest(identical_diff(suites, mutant["id"])) != record["diff_sha256"]:
                        raise SystemExit(f"S5: mutated source changed during scoring: {mutant['id']}")
            finally:
                for suite in suites:
                    restore(suite["tree"])
            out.write(json.dumps(record) + "\n")
            out.flush()


def check_current(args):
    """Score every tree exactly as it is: clean replay, or a preflight probe."""
    suites = load_suites(args.suites)
    diff = identical_diff(suites, "check")
    runs = [run_suites(suites, set(args.scope)) for _ in range(args.runs)]
    print(json.dumps({"diff_sha256": digest(diff), "runs": runs}, indent=2))


def run_challenges(args):
    """Apply each challenge patch to every suite tree, score, restore, and record."""
    suites = load_suites(args.suites)
    with open(args.out, "a") as out:
        for challenge in args.ids:
            patch = Path(args.dir, f"{challenge}.patch").resolve()
            for suite in suites:
                require_clean(suite["tree"])
            try:
                for suite in suites:
                    subprocess.run(["git", "-C", str(suite["tree"]), "apply", str(patch)], check=True,
                                   capture_output=True)
                diff = identical_diff(suites, challenge)
                record = {"id": challenge, "kind": "regression" if challenge.startswith("R") else "control",
                          "diff_sha256": digest(diff), "suites": run_suites(suites, set(args.scope))}
                if digest(identical_diff(suites, challenge)) != record["diff_sha256"]:
                    raise SystemExit(f"S5: challenge source changed during scoring: {challenge}")
            finally:
                for suite in suites:
                    restore(suite["tree"])
            out.write(json.dumps(record) + "\n")
            out.flush()


class Knockout(ast.NodeTransformer):
    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        node.body = [ast.parse(KNOCKOUT).body[0]]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def knockout_status(units, changed, blocked, errored):
    """Only observed changes count. Blocked or errored units can never make the check pass;
    they make it insufficient when they could decide the outcome."""
    if not units:
        return "insufficient-evidence"
    if changed / units >= KNOCKOUT_THRESHOLD:
        return "pass"
    if (changed + blocked + errored) / units >= KNOCKOUT_THRESHOLD:
        return "insufficient-evidence"
    return "fail"


def pytest_knockout_cases(clean_cases, knocked_cases, timed_out):
    """Units are the items that passed on the unchanged tree. Under knockout an item changes only when it
    fails with an assertion or a target exception; anything else that did not pass is errored."""
    units = sorted(name for name, case in clean_cases.items() if case["class"] == "passed")
    cases = {"changed": [], "unchanged": [], "blocked": [], "errored": []}
    reached = []
    for name in units:
        if timed_out:
            cases["blocked"].append(name)
            continue
        case = knocked_cases.get(name)
        if case is None or case["class"] not in ("passed", *KILL_CLASSES):
            cases["errored"].append(name)
        elif case["class"] == "passed":
            cases["unchanged"].append(name)
        else:
            cases["changed"].append(name)
            if case.get("knockout"):
                reached.append(name)
    return units, cases, reached


def witness_knockout_cases(suite):
    data = json.loads((suite["pack"] / "tests" / "observations.json").read_text())
    root = resolve_source_root(suite["pack"], json.loads((suite["pack"] / "manifest.json").read_text()))
    cases = {"changed": [], "unchanged": [], "blocked": [], "errored": []}
    reached = []
    for row in data["rows"]:
        purge_bytecode(suite["tree"])
        try:
            observed = run_case(root / data["legacy"], row["input"])
        except RuntimeError as exc:
            cases["blocked" if "timed out" in str(exc) else "errored"].append(row["name"])
            continue
        if observation_json(observed) == observation_json(row["legacy"]):
            cases["unchanged"].append(row["name"])
            continue
        cases["changed"].append(row["name"])
        if KNOCKOUT_MARKER in json.dumps(observed.get("exception")):
            reached.append(row["name"])
    return [row["name"] for row in data["rows"]], cases, reached


def knockout(args):
    """Adapter honesty: replace every target-scope function body with a raise, in one suite's tree.

    Witness unit: one fixture. pytest unit: one item that passed on the unchanged tree.
    changed: the unit observed the knockout (reached_target: it carries the knockout marker).
    unchanged: the unit never reached target-scope functions. blocked: timed out. errored: could not run.
    """
    (suite,) = [s for s in load_suites(args.suites) if s["name"] == args.suite]
    tree, scope = suite["tree"], set(args.scope)
    require_clean(tree)
    clean_cases = None
    if suite["kind"] == "pytest":
        purge_bytecode(tree)
        _, clean_cases, _ = run_pytest(suite, scope)
    for name in args.scope:
        path = tree / name
        path.write_text(ast.unparse(ast.fix_missing_locations(Knockout().visit(ast.parse(path.read_text())))) + "\n")
    try:
        if suite["kind"] == "witness":
            units, cases, reached = witness_knockout_cases(suite)
        else:
            purge_bytecode(tree)
            code, knocked, _ = run_pytest(suite, scope)
            units, cases, reached = pytest_knockout_cases(clean_cases, knocked, code is None)
    finally:
        restore(tree)
    counts = {kind: len(names) for kind, names in cases.items()}
    print(json.dumps({"suite": suite["name"], "kind": suite["kind"], "units": len(units), "counts": counts,
                      "reached_target": len(reached), "threshold": KNOCKOUT_THRESHOLD,
                      "status": knockout_status(len(units), counts["changed"], counts["blocked"], counts["errored"]),
                      "cases": cases}, indent=2))


def reproduce(args):
    """G4: replay one suite's deliverable from a fresh clone at a different path, in a fresh environment,
    under two hash seeds (and, for pytest, in reverse collection order). Every variant must pass."""
    (suite,) = [s for s in load_suites(args.suites) if s["name"] == args.suite]
    work = Path(args.workdir).resolve()
    if work.exists():
        raise SystemExit(f"Refusing existing workdir: {work}")
    tree = work / f"replay-{suite['name']}" / "repo"
    subprocess.run(["git", "clone", "-q", str(Path(args.repo).resolve()), str(tree)], check=True, capture_output=True)
    deliverable = Path(args.deliverable).resolve()
    shutil.copytree(deliverable, tree / deliverable.name)
    python = str(Path(args.python).absolute())
    # Works in environments built without pip (for example by uv).
    freeze = subprocess.run([python, "-c", "import importlib.metadata as m; print('\\n'.join(sorted("
                             "f'{d.metadata[\"Name\"]}=={d.version}' for d in m.distributions())))"],
                            capture_output=True, text=True)
    variants = {}
    scope = set(args.scope)
    if suite["kind"] == "pytest":
        plugins = work / "plugins"
        plugins.mkdir()
        (plugins / "cw_reverse_order.py").write_text(REVERSE_PLUGIN)
        fresh = {**suite, "tree": tree, "argv": [python, *suite["argv"][1:]]}
        for name, extra, env in (("hashseed-0", [], {"PYTHONHASHSEED": "0"}),
                                 ("hashseed-1", [], {"PYTHONHASHSEED": "1"}),
                                 ("reverse-order", ["-p", "cw_reverse_order"],
                                  {"PYTHONHASHSEED": "0", "PYTHONPATH": os.pathsep.join(
                                      p for p in (suite["env"].get("PYTHONPATH"), str(plugins)) if p)})):
            code, cases, tail = run_pytest(fresh, scope, argv=[*fresh["argv"][:3], *extra, *fresh["argv"][3:]], env=env)
            passed = code == 0 and bool(cases) and all(c["class"] in ("passed", "skipped") for c in cases.values())
            variants[name] = {"exit": code, "items": len(cases), "pass": passed, "tail": tail}
    else:
        pack = tree / suite["pack"].relative_to(suite["tree"])
        generated = pack / "tests" / "test_characterization.py"
        for seed in ("0", "1"):
            done = subprocess.run([python, str(generated)], cwd=tree, capture_output=True, text=True,
                                  env={**os.environ, "PYTHONHASHSEED": seed}, timeout=args.timeout)
            variants[f"generated-test-hashseed-{seed}"] = {"exit": done.returncode, "pass": done.returncode == 0,
                                                           "tail": (done.stdout + done.stderr)[-500:]}
        result = witness_suite({**suite, "tree": tree, "pack": pack})
        variants["scorer-replay"] = {"class": result["class"], "pass": result["class"] == "survived"}
    print(json.dumps({"suite": suite["name"], "path": str(tree), "python": python,
                      "pip_freeze": freeze.stdout.splitlines(), "variants": variants,
                      "pass": all(v["pass"] for v in variants.values())}, indent=2))


def measure_coverage(args):
    """Target-scope line and branch coverage for one suite, measured on its own tree."""
    (suite,) = [s for s in load_suites(args.suites) if s["name"] == args.suite]
    data = Path(args.data_file).resolve()
    include = "--include=" + ",".join(args.include)
    if suite["kind"] == "witness":
        command = [sys.executable, "-m", "coverage", "run", "--branch", f"--data-file={data}", include,
                   str(Path(__file__).with_name("fixture_coverage.py")), str(suite["adapter"]), str(suite["cases"])]
        env = {}
    else:
        command = [suite["argv"][0], "-m", "coverage", "run", "--branch", f"--data-file={data}", include,
                   *suite["argv"][1:]]
        env = suite["env"]
    purge_bytecode(suite["tree"])
    subprocess.run(command, cwd=suite["tree"], env={**os.environ, **env}, capture_output=True, text=True,
                   timeout=args.timeout)
    report = data.with_suffix(".json")
    subprocess.run([command[0], "-m", "coverage", "json", f"--data-file={data}", "-o", str(report)],
                   cwd=suite["tree"], check=True, capture_output=True)
    totals = json.loads(report.read_text())["totals"]
    print(json.dumps({"suite": suite["name"], "line_percent": round(100 * totals["covered_lines"] /
                                                                    max(totals["num_statements"], 1), 2),
                      "branch_percent": round(100 * totals.get("covered_branches", 0) /
                                              max(totals.get("num_branches", 0), 1), 2),
                      "totals": totals}, indent=2))
    restore(suite["tree"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("mutants", help="Enumerate and sample mutation keys")
    listing.add_argument("--tree", required=True)
    listing.add_argument("--scope", nargs="+", required=True, help="Target-scope files, relative to the tree")
    listing.add_argument("--seed", type=int, required=True)
    listing.add_argument("--cap", type=int, default=400)
    listing.add_argument("--out", required=True)
    for name, helptext in (("run", "Apply each mutant to every suite tree and score every suite"),
                           ("check", "Score every suite on its tree as it is"),
                           ("challenges", "Apply each challenge patch to every suite tree and score every suite"),
                           ("knockout", "Adapter honesty check for one suite"),
                           ("reproduce", "Replay one suite's deliverable from a fresh clone and environment"),
                           ("coverage", "Target-scope coverage for one suite")):
        command = commands.add_parser(name, help=helptext)
        command.add_argument("--suites", required=True, help="Suite declarations (JSON)")
        command.add_argument("--scope", nargs="+", required=True,
                             help="Target-scope files, relative to each tree (classifies failure origins)")
        if name == "run":
            command.add_argument("--mutants", required=True)
            command.add_argument("--out", required=True)
            command.add_argument("--start", type=int, default=0)
            command.add_argument("--stop", type=int)
            command.add_argument("--import-root", default=".")
            command.add_argument("--import-module", nargs="*", default=[])
        elif name == "challenges":
            command.add_argument("--dir", required=True, help="Directory holding <id>.patch files")
            command.add_argument("--ids", nargs="+", required=True, help="Valid challenge ids, in ID order")
            command.add_argument("--out", required=True)
        elif name == "check":
            command.add_argument("--runs", type=int, default=1)
        else:
            command.add_argument("--suite", required=True)
        if name == "reproduce":
            command.add_argument("--repo", required=True, help="Stripped target repository to clone")
            command.add_argument("--deliverable", required=True, help="Frozen deliverable directory")
            command.add_argument("--python", required=True, help="Python of the freshly built environment")
            command.add_argument("--workdir", required=True, help="New directory for the replay")
            command.add_argument("--timeout", type=float, default=600)
        if name == "coverage":
            command.add_argument("--data-file", required=True)
            command.add_argument("--include", nargs="+", required=True, help="Coverage include globs")
            command.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    {"mutants": enumerate_mutants, "run": run_mutants, "check": check_current, "challenges": run_challenges,
     "knockout": knockout, "reproduce": reproduce, "coverage": measure_coverage}[args.command](args)


if __name__ == "__main__":
    main()
