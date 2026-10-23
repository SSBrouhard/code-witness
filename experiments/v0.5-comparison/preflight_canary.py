"""Preflight P2 to P5 and P7 on the committed canary (docs/v0.5-comparison-protocol.md, section 8).

Usage: PYTHON preflight_canary.py OUTPUT_DIR
PYTHON is an environment with the pinned debt-compiler wheel, cosmic-ray, coverage and pytest.
OUTPUT_DIR must not exist. Exits nonzero if any expectation fails. No command runs through a shell.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

HERE = Path(__file__).resolve().parent
CANARY = HERE / "canary"
SCOPE = ["canary/core.py", "canary/util.py"]
PYTHON = sys.executable
CHAR_ARGV = [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "_characterization/pytest.ini",
             "--rootdir=_characterization", "--confcutdir=_characterization", "_characterization/tests"]
UPSTREAM_ARGV = [PYTHON, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests"]
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def run(*command, cwd=None, check=True):
    return subprocess.run([str(c) for c in command], cwd=cwd, capture_output=True, text=True, check=check)


def score(*args):
    done = run(PYTHON, HERE / "score.py", *args, check=False)
    if done.returncode:
        raise SystemExit(f"score.py {args[0]} failed:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}")
    return done.stdout


def commit(tree, exclude=()):
    run("git", "-C", tree, "init", "-q")
    run("git", "-C", tree, "add", "-A")
    run("git", "-C", tree, "-c", "user.name=preflight", "-c", "user.email=preflight@localhost", "commit", "-qm", "canary")
    with open(tree / ".git/info/exclude", "a") as handle:
        handle.write("".join(f"{path}\n" for path in exclude))


def witness_tree(path, adapter):
    shutil.copytree(CANARY / "repo", path)
    commit(path, ["_witness/"])
    (path / "_witness/slice/legacy").mkdir(parents=True)
    (path / "_witness/slice/fixtures").mkdir(parents=True)
    shutil.copy(CANARY / "witness" / adapter, path / "_witness/slice/legacy/batch.py")
    shutil.copy(CANARY / "witness/cases.json", path / "_witness/slice/fixtures/cases.json")
    run(Path(PYTHON).with_name("debt-compiler"), "extract", path / "_witness/slice/legacy/batch.py",
        *[path / s for s in SCOPE], "--out", path / "_witness/pack")
    return path


def characterization_tree(path, suite, decoys=True):
    shutil.copytree(CANARY / "repo", path)
    if decoys:  # Upstream configuration that must have no effect on a characterization suite.
        shutil.copy(CANARY / "decoys/conftest.py", path / "conftest.py")
        shutil.copy(CANARY / "decoys/pytest.ini", path / "pytest.ini")
    commit(path, ["_characterization/"])
    shutil.copytree(CANARY / "characterization" / suite, path / "_characterization")
    return path


def seal(out, suite):
    """Seal a suite's deliverable into an archive, as the coordinator's freeze does, and declare it."""
    name = suite.get("deliverable") or ("_witness" if suite["kind"] == "witness" else None)
    if name is None or "archive" in suite:
        return suite
    source, archive = out / suite["tree"] / name, out / "sealed" / f"{suite['name']}.tar"
    archive.parent.mkdir(exist_ok=True)
    with tarfile.open(archive, "w") as tar:
        for path in sorted(source.rglob("*")):
            if "__pycache__" not in path.parts:
                tar.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
    return {**suite, "archive": f"sealed/{suite['name']}.tar",
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}


def suites_file(out, name, suites):
    path = out / f"{name}.json"
    path.write_text(json.dumps([seal(out, s) for s in suites], indent=2) + "\n")
    return path


def main():
    out = Path(sys.argv[1]).resolve()
    if out.exists():
        raise SystemExit(f"Refusing existing output directory: {out}")
    trees = out / "trees"
    trees.mkdir(parents=True)
    # The scoring sandbox's writable scratch location, as in the protocol; runtime files go here.
    (out / "tmp").mkdir()
    os.environ["TMPDIR"] = str(out / "tmp")
    # Sentinels outside the trees must survive every scoring step.
    (out / "outside/__pycache__").mkdir(parents=True)
    (out / "outside/sentinel.txt").write_text("sentinel\n")
    (out / "outside/__pycache__/sentinel.pyc").write_text("sentinel\n")

    witness_tree(trees / "A", "batch.py")
    witness_tree(trees / "A-dishonest", "dishonest_batch.py")
    for name, suite in (("B", "honest"), ("B-dishonest", "dishonest"), ("B-self", "self_referential"),
                        ("B-order", "order_dependent"), ("B-scratch", "runtime_scratch")):
        characterization_tree(trees / name, suite)
    shutil.copytree(CANARY / "repo", trees / "upstream")
    commit(trees / "upstream")
    stripped = trees / "stripped"
    shutil.copytree(CANARY / "repo", stripped, ignore=shutil.ignore_patterns("tests"))
    commit(stripped)

    char = lambda name: {"name": name, "kind": "pytest", "tree": f"trees/{name}", "argv": CHAR_ARGV,
                         "deliverable": "_characterization", "env": {"PYTHONPATH": "."}, "timeout": 10}
    main_suites = suites_file(out, "suites", [
        {"name": "A", "kind": "witness", "tree": "trees/A"}, char("B"), char("B-self"),
        {"name": "upstream", "kind": "pytest", "tree": "trees/upstream", "argv": UPSTREAM_ARGV, "timeout": 20}])
    every = suites_file(out, "every", [
        {"name": "A", "kind": "witness", "tree": "trees/A"}, {"name": "A-dishonest", "kind": "witness", "tree": "trees/A-dishonest"},
        char("B"), char("B-dishonest"), char("B-self"), char("B-order"), char("B-scratch")])
    scope = ["--scope", *SCOPE]

    clean = json.loads(score("check", "--suites", main_suites, *scope, "--runs", "3"))
    score("mutants", "--tree", trees / "upstream", *scope, "--seed", "20261023", "--out", out / "mutants.json")
    score("run", "--suites", main_suites, *scope, "--mutants", out / "mutants.json", "--import-module", "canary.core",
          "--out", out / "results.jsonl")
    # Infrastructure fault: a missing test path must be an error, never a kill.
    infra = suites_file(out, "infra", [{"name": "missing", "kind": "pytest", "tree": "trees/upstream", "timeout": 20,
                                        "argv": [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "missing_tests"]}])
    score("run", "--suites", infra, *scope, "--mutants", out / "mutants.json", "--stop", "1", "--out", out / "infra.jsonl")

    # Stale bytecode, for a pack and for a pytest suite: warm the cache, then a same-size edit with the original mtime.
    stale = {}
    for name in ("A", "B"):
        tree = trees / name
        subprocess.run([PYTHON, "-c", "import canary.core"], cwd=tree, check=True,
                       env={k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"})
        source = tree / "canary/core.py"
        stat = source.stat()
        source.write_text(source.read_text().replace("total >= 90", "total >= 80"))
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        one = suites_file(out, f"stale-{name}", [s for s in json.loads(main_suites.read_text()) if s["name"] == name])
        stale[name] = json.loads(score("check", "--suites", one, *scope))["runs"][0][name]["class"]
        run("git", "-C", tree, "checkout", "-q", "--", ".")

    scratch = json.loads(score("check", "--suites", suites_file(out, "scratch", [char("B-scratch")]), *scope,
                               "--runs", "3"))
    scratch_files = sorted(p.name for p in (out / "tmp").rglob("*.log"))
    # Tampering between commands: an edited expectation must be refused when the next command starts.
    expected = trees / "B/_characterization/tests/expected.json"
    original = expected.read_text()
    expected.write_text(original.replace('"high"', '"mid"', 1))
    tampered = run(PYTHON, HERE / "score.py", "check", "--suites", main_suites, *scope, check=False)
    expected.write_text(original)

    knock = {name: json.loads(score("knockout", "--suites", every, *scope, "--suite", name))
             for name in ("A", "A-dishonest", "B", "B-dishonest")}
    repro = {name: json.loads(score("reproduce", "--suites", every, *scope, "--suite", name, "--repo", stripped,
                                    "--deliverable", trees / name / ("_witness" if name == "A" else "_characterization"),
                                    "--python", PYTHON, "--workdir", out / f"reproduce-{name}"))
             for name in ("A", "B", "B-order")}
    # The decoys must be live: run under the repository's own configuration, they break the honest suite.
    unguarded = run(PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "pytest.ini", "--rootdir=.",
                    "_characterization/tests", cwd=trees / "B", check=False)
    cover = {name: json.loads(score("coverage", "--suites", every, *scope, "--suite", name,
                                    "--data-file", out / f"cov-{name}", "--include", *SCOPE)) for name in ("A", "B")}

    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    valid = [r for r in rows if r["validity"] == "valid"]
    classes = lambda suite: [r["suites"][suite]["class"] for r in valid]
    kinds = lambda suite: {c for r in valid for c in r["suites"][suite].get("tests", {}).values()}
    infra_class = json.loads((out / "infra.jsonl").read_text())["suites"]["missing"]["class"]
    dirty = {name: run("git", "-C", trees / name, "status", "--porcelain", "--untracked-files=all").stdout
             for name in ("A", "A-dishonest", "B", "B-dishonest", "B-self", "B-order", "B-scratch", "upstream")}
    checks = {
        "P2 clean replay passes 3/3 for every suite": all(r[s]["class"] == "survived" for r in clean["runs"] for s in r),
        # score.py stops (S5) if any tree's diff differs, so a complete run means identical mutations everywhere.
        "P4 every mutant was applied to every tree with an identical diff and scored": len(rows) == json.loads((out / "mutants.json").read_text())["total"] and bool(valid),
        "witness, characterization and upstream suites kill mutants": all("behavioral" in classes(s) for s in ("A", "B", "upstream")),
        "kill sets differ between suites": len({tuple(classes(s)) for s in ("A", "B", "upstream")}) > 1,
        "infinite-loop mutant is a timeout, not a kill": "timeout" in classes("A") and "timeout" in classes("B"),
        "no unexplained errors on canary mutants": "error" not in classes("A") + classes("B") + classes("upstream"),
        "missing test path is an error, not a kill": infra_class == "error",
        "P3 stale-bytecode edit is detected (pack and pytest suite)": stale == {"A": "behavioral", "B": "behavioral"},
        "P5 honest adapter: knockout passes": knock["A"]["status"] == "pass",
        "P5 dishonest adapter: knockout fails": knock["A-dishonest"]["status"] == "fail",
        "P7 honest pytest suite: knockout passes": knock["B"]["status"] == "pass",
        "P7 dishonest pytest suite: knockout fails": knock["B-dishonest"]["status"] == "fail",
        "P7 self-referential pytest suite kills no mutant": "behavioral" not in classes("B-self"),
        "P7 pytest target exceptions classified, nothing unclassified": "target-exception" in kinds("B") | kinds("upstream")
            and "unclassified" not in kinds("B") | kinds("upstream"),
        "P7 reproduction passes for the honest pack and suite": repro["A"]["pass"] and repro["B"]["pass"],
        "P7 order-dependent suite fails only in reverse order": not repro["B-order"]["pass"]
            and repro["B-order"]["variants"]["hashseed-0"]["pass"] and not repro["B-order"]["variants"]["reverse-order"]["pass"],
        "P7 decoy conftest.py and pytest.ini have no effect with the fixed command": clean["runs"][0]["B"]["items"] == 12,
        "P7 decoys are live without the fixed command": unguarded.returncode != 0,
        "runtime scratch: a suite writing under TMPDIR passes 3/3 with its deliverable unchanged":
            all(r["B-scratch"]["class"] == "survived" for r in scratch["runs"]) and len(scratch_files) == 36,
        "freeze binding: scoring refuses a deliverable edited between commands": tampered.returncode != 0
            and "working copy differs from the sealed archive" in tampered.stderr,
        "freeze binding: check records the verified archive hashes":
            set(clean["deliverables"]) == {"A", "B", "B-self"},
        "coverage measured for pack and suite": all(c["line_percent"] > 0 for c in cover.values()),
        "trees restored after scoring": not any(dirty.values()),
        "sentinels outside the trees intact": (out / "outside/sentinel.txt").read_text() == "sentinel\n"
            and (out / "outside/__pycache__/sentinel.pyc").is_file(),
    }
    summary = {"pass": all(checks.values()), "checks": checks, "mutants": len(rows), "valid": len(valid),
               "classes": {s: {c: classes(s).count(c) for c in sorted(set(classes(s)))} for s in ("A", "B", "B-self", "upstream")},
               "knockout": {n: k["counts"] for n, k in knock.items()},
               "reproduce": {n: {v: r["variants"][v]["pass"] for v in r["variants"]} for n, r in repro.items()},
               "coverage": {n: [c["line_percent"], c["branch_percent"]] for n, c in cover.items()}}
    (out / "preflight-canary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary["pass"] else 1)


if __name__ == "__main__":
    main()
