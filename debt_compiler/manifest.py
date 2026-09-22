"""Portable source locations: repository-relative files, pack-relative defaults."""
from datetime import datetime, timezone
from importlib.metadata import version
import os
from pathlib import Path

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


def build(files, root, out, classification):
    cwd = Path.cwd().resolve()
    return {
        "format_version": 1,
        "tool_version": version("debt-compiler"),
        "extract_time": datetime.now(timezone.utc).isoformat(),
        "default_source_root": relative(cwd, out),
        "source_roots": [{"path": relative(root, cwd), "git_head": git(root, "rev-parse", "--verify", "HEAD")}],
        "source_files": [relative(p, root) for p in files],
        "exit_classification": classification,
        "exit_code": 1 if classification == "mismatch" else 0,
    }


def resolve_source_root(pack, manifest, override=None):
    if override is not None:
        return Path(override).resolve()
    # The recorded invocation directory is relative to the pack, never absolute.
    return (pack / manifest["default_source_root"] / manifest["source_roots"][0]["path"]).resolve()
