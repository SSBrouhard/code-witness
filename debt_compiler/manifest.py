"""Portable source locations: repository-relative files, pack-relative defaults."""
from datetime import datetime, timezone
from importlib.metadata import version
import hashlib
import os
from pathlib import Path
import platform
import sys

from .archaeology import git


def relative(path, root):
    return Path(os.path.relpath(path, root)).as_posix()


def source_root(files):
    roots = {git(p.parent, "rev-parse", "--show-toplevel") for p in files}
    if len(roots) == 1 and None not in roots:
        return Path(roots.pop()).resolve()
    if any(root is not None for root in roots):
        raise ValueError("Select a slice within one repository, or only non-repository files")
    cwd = Path.cwd().resolve()
    if all(p.is_relative_to(cwd) for p in files):
        return cwd
    return Path(os.path.commonpath([p.parent for p in files]))


def fingerprints(files, root):
    """Hash selected sources plus the explicit runtime targets and fixture bytes.

    Imported dependencies and external resources are outside this bounded set.
    """
    sources = set(files)
    fixtures = set()
    for path in files:
        if path.name == "batch.py":
            fixture = path.parent.parent / "fixtures/cases.json"
            if fixture.is_file():
                fixtures.add(fixture)
                modern = path.parent.parent / "modern/batch.py"
                if modern.is_file():
                    sources.add(modern)
    def hashes(paths):
        return {relative(p, root): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(paths)}
    return {"algorithm": "sha256", "sources": hashes(sources), "fixtures": hashes(fixtures)}


def provenance(files, root):
    head = git(root, "rev-parse", "--verify", "HEAD")
    status = git(root, "status", "--porcelain", "--untracked-files=normal")
    return {"fingerprints": fingerprints(files, root), "git_head": head,
            "git_dirty": bool(status) if status is not None else None}


def build(files, root, out, classification, evidence, verification, exit_code):
    return {
        "format_version": 2,
        "tool_version": version("debt-compiler"),
        "extract_time": datetime.now(timezone.utc).isoformat(),
        "default_source_root": relative(root, out),
        "source_roots": [{"path": ".", "git_head": evidence["git_head"],
                          "git_dirty": evidence["git_dirty"]}],
        "source_files": [relative(p, root) for p in files],
        "fingerprints": evidence["fingerprints"],
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "python_cache_tag": sys.implementation.cache_tag,
            "system": platform.system(), "machine": platform.machine(),
            "dependencies": {name: version(name) for name in ("tree-sitter", "tree-sitter-python")},
            "tool_source_sha256": hashlib.sha256(b"".join(
                p.name.encode() + b"\0" + p.read_bytes() + b"\0"
                for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest(),
        },
        "verification": verification,
        "exit_classification": classification,
        "exit_code": exit_code,
    }


def resolve_source_root(pack, manifest, override=None):
    if override is not None:
        return Path(override).resolve()
    # The recorded invocation directory is relative to the pack, never absolute.
    return (pack / manifest["default_source_root"] / manifest["source_roots"][0]["path"]).resolve()
