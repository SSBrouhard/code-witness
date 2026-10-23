"""Run each frozen fixture through the adapter in-process so coverage sees the target.

Usage: coverage run --branch --include=<target scope> fixture_coverage.py ADAPTER CASES
"""
import importlib.util
import json
from pathlib import Path
import sys

adapter, cases = Path(sys.argv[1]).resolve(), json.loads(Path(sys.argv[2]).read_text())
sys.path.insert(0, str(adapter.parent))
spec = importlib.util.spec_from_file_location("adapter", adapter)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for case in cases:
    try:
        module.run(case["input"], sleep=lambda seconds: None)
    except Exception:
        pass
