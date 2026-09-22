"""Deterministic extraction. No model or network calls."""


def extract(paths, out="debt-pack"):
    """Write a contract pack for a path or iterable of paths; return its summary."""
    from .cli import extract as extract_pack
    return extract_pack(paths, out)
