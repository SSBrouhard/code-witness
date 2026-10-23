"""Isolation probe (preflight P1). Run it inside the exact sandbox a role's commands use.

Prints JSON and exits 1 if any probe is unexpected or untested:
  python probe.py --workspace DIR --deny-read PATH... --deny-write DIR... --deny-keychain SERVICE...
Never prints secret values: environment variables are reported by name only,
and keychain probes query item existence without -w.
"""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import uuid

CREDENTIAL_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|API_?KEY|AUTH|CREDENTIAL|COOKIE|SESSION_KEY", re.I)


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


def proxied():
    """Egress through any proxy the runtime injected (HTTP(S)_PROXY, ALL_PROXY) must also fail."""
    import urllib.request
    urllib.request.urlopen("https://example.com/", timeout=10).close()


CHILD = ("import socket; socket.create_connection(('1.1.1.1', 443), timeout=5).close()")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--deny-read", nargs="*", default=[])
    parser.add_argument("--deny-write", nargs="*", default=[])
    parser.add_argument("--deny-keychain", nargs="*", default=[], help="macOS keychain service names")
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    rows = [("workspace read", read(workspace), "allowed"), ("workspace write", write(workspace), "allowed"),
            ("network", connect, "denied"), ("network via injected proxy", proxied, "denied")]
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
    exposed = sorted(name for name in os.environ if CREDENTIAL_NAME.search(name))
    ok &= not exposed
    results.append({"probe": "credential-like environment variables", "expected": "none",
                    "observed": exposed or "none", "pass": not exposed})
    for service in args.deny_keychain:
        try:
            found = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service],
                                   capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            found = False
        ok &= not found
        results.append({"probe": f"keychain item {service}", "expected": "denied",
                        "observed": "readable" if found else "denied", "pass": not found})
    print(json.dumps({"pass": ok, "results": results}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
