"""Read-only Git context. Missing history is data, not a fatal error."""
import subprocess


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                            text=True, timeout=10)
    if result.returncode:
        return None
    return result.stdout.strip()


def context(path, line):
    try:
        path = path.resolve()
        root = git(path.parent, "rev-parse", "--show-toplevel")
        if not root:
            return {"status": "unavailable", "reason": "Not a Git repository"}
        from pathlib import Path
        relative = path.relative_to(Path(root)).as_posix()
        blame = git(root, "blame", "--line-porcelain", "-L", f"{line},{line}", "--", relative)
        if not blame:
            return {"status": "unavailable", "reason": "File is untracked or has no blame history"}
        commit = blame.split()[0]
        if set(commit) == {"0"}:
            return {"status": "uncommitted", "reason": "Flagged line is not committed"}
        subject = git(root, "log", "-1", "--format=%s", commit)
        return {"status": "available", "commit": commit, "subject": subject,
                "file": relative, "line": line,
                "source_link": f"{relative}#L{line}",
                "blame": ["git", "blame", "-L", f"{line},{line}", commit, "--", relative],
                "show": ["git", "show", commit, "--", relative]}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
