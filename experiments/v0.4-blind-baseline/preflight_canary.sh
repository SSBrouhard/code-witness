#!/bin/bash
# Preflight P2-P5 on the committed canary (docs/v0.4-blind-baseline-protocol.md).
# Usage: preflight_canary.sh VENV OUTPUT_DIR
# VENV has the pinned debt-compiler wheel, cosmic-ray, coverage and pytest.
# OUTPUT_DIR must not exist. Exits nonzero if any expectation fails.
set -euo pipefail

venv=$(cd "$1" && pwd)
python="$venv/bin/python"
here=$(cd "$(dirname "$0")" && pwd)
[ ! -e "$2" ] || { echo "Refusing existing output directory: $2" >&2; exit 2; }
mkdir -p "$2"
out=$(cd "$2" && pwd)
export PYTHONDONTWRITEBYTECODE=1

tree="$out/tree"
cp -R "$here/canary/repo" "$tree"
git -C "$tree" init -q
git -C "$tree" add -A
git -C "$tree" -c user.name=preflight -c user.email=preflight@localhost commit -qm canary
echo "_witness/" >> "$tree/.git/info/exclude"

# Sentinels outside the tree must survive every scoring step.
mkdir -p "$out/outside/__pycache__"
echo sentinel > "$out/outside/sentinel.txt"
echo sentinel > "$out/outside/__pycache__/sentinel.pyc"

extract() {
  rm -rf "$tree/_witness"
  mkdir -p "$tree/_witness/slice/legacy" "$tree/_witness/slice/fixtures"
  cp "$here/canary/witness/$1" "$tree/_witness/slice/legacy/batch.py"
  cp "$here/canary/witness/cases.json" "$tree/_witness/slice/fixtures/cases.json"
  "$venv/bin/debt-compiler" extract "$tree/_witness/slice/legacy/batch.py" "$tree/canary/core.py" \
    "$tree/canary/util.py" --out "$tree/_witness/pack" > "$out/extract-$1.json"
}

score() { "$python" "$here/score.py" "$@"; }
upstream=$(printf '["%s", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests"]' "$python")

extract batch.py
score check --tree "$tree" --scope canary/core.py canary/util.py --pack "$tree/_witness/pack" --upstream "$upstream" --runs 3 > "$out/clean.json"
score mutants --tree "$tree" --scope canary/core.py canary/util.py --seed 20261023 --out "$out/mutants.json"
score run --tree "$tree" --scope canary/core.py canary/util.py --pack "$tree/_witness/pack" --upstream "$upstream" --mutants "$out/mutants.json" \
  --import-module canary.core --out "$out/results.jsonl"

# Infrastructure fault: a missing upstream test path must be an error, never a kill.
bad=$(printf '["%s", "-m", "pytest", "-q", "-p", "no:cacheprovider", "missing_tests"]' "$python")
score run --tree "$tree" --scope canary/core.py canary/util.py --suites upstream --upstream "$bad" --mutants "$out/mutants.json" --stop 1 \
  --out "$out/infra.jsonl"

# Stale bytecode: warm the cache, then make a same-size edit with the original mtime.
(cd "$tree" && env -u PYTHONDONTWRITEBYTECODE "$python" -c "import canary.core")
touch -r "$tree/canary/core.py" "$out/mtime"
sed -i '' 's/total >= 90/total >= 80/' "$tree/canary/core.py"
touch -r "$out/mtime" "$tree/canary/core.py"
# Control (informational): the generated test without a cache purge may replay the stale module.
if "$python" "$tree/_witness/pack/tests/test_characterization.py" > "$out/stale-control.txt" 2>&1; then
  echo masked > "$out/stale-control.result"
else
  echo detected > "$out/stale-control.result"
fi
score check --tree "$tree" --suites witness --pack "$tree/_witness/pack" > "$out/stale.json"
git -C "$tree" checkout -q -- .

score knockout --tree "$tree" --scope canary/core.py canary/util.py --pack "$tree/_witness/pack" > "$out/knockout-honest.json"
extract dishonest_batch.py
score knockout --tree "$tree" --scope canary/core.py canary/util.py --pack "$tree/_witness/pack" > "$out/knockout-dishonest.json"

"$python" - "$out" "$tree" <<'EOF'
import json, subprocess, sys
from pathlib import Path
out, tree = Path(sys.argv[1]), sys.argv[2]
load = lambda name: json.loads((out / name).read_text())
rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
valid = [r for r in rows if r["validity"] == "valid"]
classes = lambda suite: [r["suites"][suite]["class"] for r in valid]
infra = json.loads((out / "infra.jsonl").read_text())["suites"]["upstream"]
upstream_tests = {c for r in valid for c in r["suites"]["upstream"]["tests"].values()}
honest, dishonest = load("knockout-honest.json"), load("knockout-dishonest.json")
status = subprocess.run(["git", "-C", tree, "status", "--porcelain", "--untracked-files=all"],
                        capture_output=True, text=True).stdout
checks = {
    "clean replay passes 3/3 for both suites": all(run[s]["class"] == "survived" for run in load("clean.json")["runs"] for s in run),
    "every mutant was applied and scored": len(rows) == load("mutants.json")["total"] and bool(valid),
    "both suites kill some mutants behaviorally": "behavioral" in classes("witness") and "behavioral" in classes("upstream"),
    "kill sets differ between suites": classes("witness") != classes("upstream"),
    "infinite-loop mutant is a timeout, not a kill": "timeout" in classes("witness"),
    "no unexplained errors on canary mutants": "error" not in classes("witness") + classes("upstream"),
    "missing test path is an error, not a kill": infra["class"] == "error",
    "stale-bytecode edit is detected": load("stale.json")["runs"][0]["witness"]["class"] == "behavioral",
    "honest adapter: knockout passes on observed changes": honest["status"] == "pass" and honest["counts"]["changed"] == honest["fixtures"],
    "dishonest adapter: knockout fails, no observed change": dishonest["status"] == "fail" and dishonest["counts"]["changed"] == 0,
    "upstream target exceptions classified, nothing unclassified": "target-exception" in upstream_tests and "unclassified" not in upstream_tests,
    "tree restored after scoring": status == "",
    "sentinels outside the tree intact": (out / "outside/sentinel.txt").read_text() == "sentinel\n"
        and (out / "outside/__pycache__/sentinel.pyc").is_file(),
}
summary = {"pass": all(checks.values()), "checks": checks, "mutants": len(rows), "valid": len(valid),
           "stale_control_without_purge": (out / "stale-control.result").read_text().strip(),
           "witness": {c: classes("witness").count(c) for c in set(classes("witness"))},
           "upstream": {c: classes("upstream").count(c) for c in set(classes("upstream"))}}
(out / "preflight-canary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
sys.exit(0 if summary["pass"] else 1)
EOF
