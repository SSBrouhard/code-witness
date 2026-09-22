"""Compare contract facts, not extraction timestamps or machine locations."""
import json
from pathlib import Path


def read(pack, name):
    return json.loads((pack / name).read_text())


def keyed(items, key):
    result = {}
    for item in items:
        identity = key(item)
        if identity in result:
            raise ValueError(f"Duplicate contract identity: {identity}")
        result[identity] = item
    return result


def snapshot(pack):
    pack = Path(pack)
    manifest = read(pack, "manifest.json")
    if manifest.get("format_version") != 1:
        raise ValueError("Unsupported pack format; re-extract with this version")
    # Git commit metadata is provenance, not a change to the heuristic contract.
    annotations = [{k: a[k] for k in ("file", "line", "tag", "risk", "snippet", "reason")}
                   for a in read(pack, "annotations.json")]
    invariants = read(pack, "invariants.json")
    for rule in invariants:
        for field in ("evidence", "fixture_checks"):
            if field in rule:
                rule[field] = sorted(rule[field], key=lambda v: json.dumps(v, sort_keys=True))
    runtime = read(pack, "tests/observations.json")
    cases = [{"name": row["name"], "verdict": "MATCH" if row["equal"] is True else "MISMATCH" if row["equal"] is False else "LEGACY ONLY"}
             for row in runtime["rows"]]
    if runtime["status"] == "not-run":
        cases = [{"name": "<runtime>", "verdict": "NOT RUN"}]
    buckets = {}
    bucket = None
    for line in (pack / "risk.md").read_text().splitlines():
        if line.startswith("## "):
            bucket = line[3:]
            buckets[bucket] = []
        elif bucket and line.strip():
            buckets[bucket].append(line.strip())
    if set(buckets) != {"Touch", "Do-not-touch", "Needs-human"}:
        raise ValueError("risk.md is missing the expected risk buckets")
    return {
        "annotations": keyed(annotations, lambda a: f"{a['file']}:{a['line']} [{a['tag']}]"),
        "invariants": keyed(invariants, lambda r: r["id"]),
        "dual-run cases": keyed(cases, lambda r: r["name"]),
        "risk buckets": {key: sorted(value) for key, value in buckets.items()},
    }


def compare(old_pack, new_pack):
    old, new = snapshot(old_pack), snapshot(new_pack)
    changed = False
    lines = []
    for category in old:
        left, right = old[category], new[category]
        added = sorted(right.keys() - left.keys())
        removed = sorted(left.keys() - right.keys())
        modified = sorted(key for key in left.keys() & right.keys() if left[key] != right[key])
        if not (added or removed or modified):
            lines.append(f"{category}: unchanged ({len(left)})")
            continue
        changed = True
        lines.append(f"{category}: +{len(added)} -{len(removed)} changed {len(modified)}")
        details = [f"+ {k}" for k in added] + [f"- {k}" for k in removed] + [f"~ {k}" for k in modified]
        lines.extend(f"  {detail}" for detail in details[:5])
        if len(details) > 5:
            lines.append(f"  ... {len(details) - 5} more")
    lines.insert(0, "Contract changes detected." if changed else "Identical-enough: no contract changes.")
    return changed, "\n".join(lines)
