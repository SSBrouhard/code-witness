"""v0.4 scorer: identical mutants for both suites, separate outcome classes, verified restoration.

Deterministic support for docs/v0.4-blind-baseline-protocol.md. Not part of debt-compiler.
No command here runs through a shell; cosmic-ray is used only to enumerate and apply mutants.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import random
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
KNOCKOUT = 'raise RuntimeError("code-witness knockout")'


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
        raise SystemExit(f"S5: tree is not clean:\n{status}")


def restore(tree):
    git(tree, "checkout", "--", ".")
    require_clean(tree)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def classify(behavioral, timeouts, errors):
    """Only an observed behavioral difference counts as a kill."""
    if behavioral:
        return "behavioral"
    if timeouts:
        return "timeout"
    if errors:
        return "error"
    return "survived"


def witness_suite(pack):
    """Same comparison as the pack's generated test, reported per fixture."""
    pack = Path(pack).resolve()
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


def upstream_suite(tree, argv, timeout):
    """pytest junit: <failure> is behavioral; setup/collection <error> or other exits are not kills."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp, "junit.xml")
        try:
            done = subprocess.run([*argv, f"--junitxml={report}"], cwd=tree, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"class": "timeout", "behavioral": [], "timeouts": [f"suite exceeded {timeout}s"], "errors": []}
        failures, errors = [], []
        if report.is_file():
            for case in ET.parse(report).getroot().iter("testcase"):
                name = f"{case.get('classname')}::{case.get('name')}"
                if case.find("failure") is not None:
                    failures.append(name)
                elif case.find("error") is not None:
                    errors.append(name)
        if done.returncode and not failures:
            errors.append(f"exit {done.returncode}: {(done.stdout + done.stderr)[-500:]}")
        if not done.returncode and (failures or errors):
            errors.append("exit 0 with recorded failures or errors")
    return {"class": classify(failures, [], errors), "behavioral": failures, "timeouts": [], "errors": errors,
            "exit": done.returncode}


def run_suites(args, tree):
    """Run each selected suite; an error result is rerun once before it is final."""
    results = {}
    for suite in args.suites:
        attempts = []
        for _ in range(2):
            start = time.monotonic()
            purge_bytecode(tree)
            result = (witness_suite(args.pack) if suite == "witness"
                      else upstream_suite(tree, args.upstream, args.upstream_timeout))
            result["seconds"] = round(time.monotonic() - start, 3)
            attempts.append(result)
            if result["class"] != "error":
                break
        results[suite] = {**attempts[-1], "attempts": len(attempts)}
    return results


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
        keys.append({"path": Path(mutation["module_path"]).relative_to(tree).as_posix(),
                     "operator": mutation["operator_name"], "occurrence": mutation["occurrence"],
                     "line": mutation["start_pos"][0], "definition": mutation.get("definition_name")})
    keys.sort(key=lambda k: (k["path"], k["operator"], k["occurrence"]))
    selected = keys if len(keys) <= args.cap else sorted(
        random.Random(args.seed).sample(keys, args.cap), key=lambda k: (k["path"], k["operator"], k["occurrence"]))
    Path(args.out).write_text(json.dumps({"scope": args.scope, "total": len(keys), "seed": args.seed,
                                          "cap": args.cap, "selected": selected}, indent=2) + "\n")
    print(json.dumps({"total": len(keys), "selected": len(selected)}))


def run_mutants(args):
    tree = Path(args.tree).resolve()
    mutants = json.loads(Path(args.mutants).read_text())["selected"][args.start:args.stop]
    with open(args.out, "a") as out:
        for mutant in mutants:
            require_clean(tree)
            applied = subprocess.run([COSMIC, "apply", str(tree / mutant["path"]), mutant["operator"],
                                      str(mutant["occurrence"])], capture_output=True, text=True)
            try:
                diff = git(tree, "diff") if not applied.returncode else ""
                record = {**mutant, "diff_sha256": digest(diff), "diff": diff, "validity": validity(args, tree, diff)}
                if record["validity"] == "valid":
                    record["suites"] = run_suites(args, tree)
                    # Both suites must have observed byte-identical mutated source.
                    if digest(git(tree, "diff")) != record["diff_sha256"]:
                        raise SystemExit(f"S5: mutated source changed during scoring: {mutant}")
            finally:
                restore(tree)
            out.write(json.dumps(record) + "\n")
            out.flush()


def check_current(args):
    """Score the tree exactly as it is: clean replay, an applied challenge patch, or a preflight probe."""
    tree = Path(args.tree).resolve()
    runs = [run_suites(args, tree) for _ in range(args.runs)]
    print(json.dumps({"diff_sha256": digest(git(tree, "diff")), "runs": runs}, indent=2))


class Knockout(ast.NodeTransformer):
    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        node.body = [ast.parse(KNOCKOUT).body[0]]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def knockout(args):
    """Adapter honesty: replace every target-scope function body with a raise.

    A fixture whose observation does not change never reached target-scope functions.
    """
    tree = Path(args.tree).resolve()
    require_clean(tree)
    for name in args.scope:
        path = tree / name
        path.write_text(ast.unparse(ast.fix_missing_locations(Knockout().visit(ast.parse(path.read_text())))) + "\n")
    try:
        pack = Path(args.pack).resolve()
        data = json.loads((pack / "tests" / "observations.json").read_text())
        root = resolve_source_root(pack, json.loads((pack / "manifest.json").read_text()))
        changed, unchanged, errored = [], [], []
        for row in data["rows"]:
            purge_bytecode(tree)
            try:
                observed = run_case(root / data["legacy"], row["input"])
            except RuntimeError:
                errored.append(row["name"])
                continue
            (unchanged if observation_json(observed) == observation_json(row["legacy"]) else changed).append(row["name"])
    finally:
        restore(tree)
    print(json.dumps({"fixtures": len(data["rows"]), "changed": len(changed), "errored": errored,
                      "unchanged": unchanged}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("mutants", help="Enumerate and sample mutation keys")
    listing.add_argument("--tree", required=True)
    listing.add_argument("--scope", nargs="+", required=True, help="Target-scope files, relative to the tree")
    listing.add_argument("--seed", type=int, required=True)
    listing.add_argument("--cap", type=int, default=400)
    listing.add_argument("--out", required=True)
    for name, helptext in (("run", "Apply each mutant once and score the selected suites against it"),
                           ("check", "Score the selected suites on the tree as it is")):
        command = commands.add_parser(name, help=helptext)
        command.add_argument("--tree", required=True)
        command.add_argument("--suites", nargs="+", choices=("witness", "upstream"), default=["witness", "upstream"])
        command.add_argument("--pack", help="Frozen contract pack (required for the witness suite)")
        command.add_argument("--upstream", type=json.loads, default=[],
                             help='Upstream pytest argv as a JSON list, e.g. ["python", "-m", "pytest", "-q", "tests"]')
        command.add_argument("--upstream-timeout", type=float, default=300)
        if name == "run":
            command.add_argument("--mutants", required=True)
            command.add_argument("--out", required=True)
            command.add_argument("--start", type=int, default=0)
            command.add_argument("--stop", type=int)
            command.add_argument("--import-root", default=".")
            command.add_argument("--import-module", nargs="*", default=[])
        else:
            command.add_argument("--runs", type=int, default=1)
    honesty = commands.add_parser("knockout", help="Adapter honesty check against target-scope functions")
    honesty.add_argument("--tree", required=True)
    honesty.add_argument("--scope", nargs="+", required=True)
    honesty.add_argument("--pack", required=True)
    args = parser.parse_args(argv)
    if args.command in ("run", "check"):
        if "witness" in args.suites and not args.pack:
            parser.error("--pack is required for the witness suite")
        if "upstream" in args.suites and not args.upstream:
            parser.error("--upstream is required for the upstream suite")
    {"mutants": enumerate_mutants, "run": run_mutants, "check": check_current, "knockout": knockout}[args.command](args)


if __name__ == "__main__":
    main()
