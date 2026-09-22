"""debt-compiler extract PATH [PATH ...] --out DIRECTORY"""
import argparse
import json
from pathlib import Path
import sys
import tempfile

from .parse import discover, parse
from .manifest import source_root, build as build_manifest
from .diff import compare
from .graph import Graph
from .flags import scan
from .invariants import extract as extract_invariants
from .harness import execute, report, write_tests
from .jev import review as jev_review


def extract(paths, out="debt-pack"):
    files = discover(paths)
    destination = Path(out).resolve()
    # Never overwrite a selected source file via an output artifact.
    if any(destination == p.parent or destination in p.parents for p in files):
        raise ValueError("Output must not contain the selected source files")
    root = source_root(files)
    sources = parse(files, root)
    graph = Graph.build(sources)
    annotations = scan(sources)
    rules = extract_invariants(sources)
    result = execute(files, rules, root)
    for rule in rules:
        rule["fixture_checks"] = [{"fixture": row["name"], **row["checks"][rule["id"]]} for row in result["rows"]]
        rule["status"] = "candidate-fixture-checked" if any(r["legacy"] == "pass" for r in rule["fixture_checks"]) else "candidate-unverified"
        if any("fail" in (r["legacy"], r["modern"]) for r in rule["fixture_checks"]):
            rule["status"] = "candidate-violated"
    destination.mkdir(parents=True, exist_ok=True)
    # Jev artifacts are advisory evidence for this exact extraction only.
    review_artifact = destination / "jev_review.json"
    if review_artifact.exists():
        review_artifact.unlink()
    (destination / "graph.md").write_text(graph.markdown())
    (destination / "annotations.json").write_text(json.dumps(annotations, indent=2) + "\n")
    (destination / "invariants.json").write_text(json.dumps(rules, indent=2) + "\n")
    (destination / "dual_run.md").write_text(report(result))
    write_tests(destination, result)
    risk = ["# Risk", "", "## Touch", "", "Documentation and generated contract-pack artifacts. No source transformation is performed.", "", "## Do-not-touch", ""]
    for a in annotations:
        if a["risk"] == "do-not-touch":
            risk.append(f"- `{a['location']}` ({a['tag']}): {a['reason']}")
    risk += ["", "## Needs-human", "", "All code changes require review. Fixture agreement is limited evidence, not equivalence proof. Candidate invariants require approval; missing history is not permission to remove workarounds.", ""]
    for a in annotations:
        if a["risk"] == "needs-human":
            risk.append(f"- `{a['location']}` ({a['tag']}): {a['reason']}")
    if result["status"] == "not-run":
        risk.append(f"- No runtime characterization: {result['reason']}")
    failed = any(row["equal"] is False or any("fail" in values.values() for values in row["checks"].values()) for row in result["rows"])
    if failed:
        risk.append("- FAILED: runtime differences or invariant violations exist; inspect dual_run.md.")
    (destination / "risk.md").write_text("\n".join(risk) + "\n")
    classification = "mismatch" if failed else "runtime-skipped" if result["status"] == "not-run" else "legacy-only" if result["modern"] is None else "match"
    manifest = build_manifest(files, root, destination, classification)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"out": str(destination), "files": len(files), "fixtures": len(result["rows"]), "failed": failed}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract a deterministic Python contract pack")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("extract", help="Extract a bounded Python directory or file set")
    command.add_argument("paths", nargs="+")
    command.add_argument("--out", default="debt-pack")
    diff_command = commands.add_parser("diff", help="Compare two extracted contract packs")
    diff_command.add_argument("old_pack")
    diff_command.add_argument("new_pack")
    jev_command = commands.add_parser("jev-review", help="Ask Jev to assess evidence already present in a contract pack")
    jev_command.add_argument("pack")
    jev_command.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    try:
        if args.command == "diff":
            changed, human_report = compare(args.old_pack, args.new_pack)
            print(human_report)
            return int(changed)
        if args.command == "jev-review":
            pack = Path(args.pack)
            reviewed = jev_review(pack, timeout=args.timeout)
            output = pack / "jev_review.json"
            with tempfile.NamedTemporaryFile("w", dir=pack, delete=False) as artifact:
                artifact.write(json.dumps(reviewed, indent=2) + "\n")
                temporary_output = Path(artifact.name)
            temporary_output.replace(output)
            print(json.dumps({"out": str(output), "claims": len(reviewed["claims"]), "model": reviewed["model"]}))
            return 0
        summary = extract(args.paths, args.out)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"debt-compiler: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary))
    return 1 if summary["failed"] else 0
