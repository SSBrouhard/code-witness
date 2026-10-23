"""Isolation probe (preflight P1). Run it inside the exact sandbox a role will use.

Prints JSON and exits 1 if any probe is unexpected or untested:
  python probe.py --workspace DIR --deny-read PATH... --deny-write DIR...
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid


def attempt(action):
    # Never stat a target first: under a read-deny profile even existence checks fail.
    try:
        action()
    except FileNotFoundError:
        return "untested: path does not exist"
    except OSError as exc:
        return f"denied: {type(exc).__name__}: {exc}"[:200]
    return "allowed"


def read(path):
    def action():
        if path.is_dir():
            entries = os.listdir(path)
            for entry in entries[:3]:
                child = path / entry
                if child.is_file():
                    child.open("rb").read(1)
        else:
            path.open("rb").read(1)
    return action


def write(directory):
    def action():
        probe = directory / f".isolation-probe-{uuid.uuid4().hex}"
        probe.write_text("probe")
        probe.unlink()
    return action


def connect():
    socket.getaddrinfo("example.com", 443)
    socket.create_connection(("1.1.1.1", 443), timeout=5).close()


CHILD = ("import socket; socket.create_connection(('1.1.1.1', 443), timeout=5).close()")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--deny-read", nargs="*", default=[])
    parser.add_argument("--deny-write", nargs="*", default=[])
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    rows = [("workspace read", read(workspace), "allowed"), ("workspace write", write(workspace), "allowed"),
            ("network", connect, "denied")]
    rows += [(f"read {p}", read(Path(p)), "denied") for p in args.deny_read]
    rows += [(f"write {p}", write(Path(p)), "denied") for p in args.deny_write]
    results, ok = [], True
    for name, action, expected in rows:
        outcome = attempt(action)
        passed = outcome.startswith(expected)
        ok &= passed
        results.append({"probe": name, "expected": expected, "observed": outcome, "pass": passed})
    child = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True)
    passed = child.returncode != 0
    ok &= passed
    results.append({"probe": "child process network", "expected": "denied",
                    "observed": "denied" if passed else "allowed", "pass": passed})
    print(json.dumps({"pass": ok, "results": results}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
