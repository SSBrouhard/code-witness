"""Generated from observed legacy outputs, not hand-authored expected behavior."""
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
