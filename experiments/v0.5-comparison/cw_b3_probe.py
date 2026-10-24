"""B3-only envelope around the unchanged engine worker. No experiment evidence is read here."""
import json
from pathlib import Path
import sys
import traceback

from debt_compiler.harness import worker


class KnockoutTrace:
    """Scoped knockout exception evidence, with imports kept separate from item execution."""
    def __init__(self, scope, adapter=None):
        self.files = {str(Path(p).resolve()) for p in scope}
        self.adapter = str(Path(adapter).resolve()) if adapter else None
        self.phase = "collection"
        self.unit = None
        self.events = []
        self.seen = []

    def trace(self, frame, event, arg):
        if frame.f_code.co_filename not in self.files:
            return None
        if event == "exception":
            kind, exc, tb = arg
            while tb.tb_next:
                tb = tb.tb_next
            if kind.__name__ == "CodeWitnessKnockout" and str(exc) == "code-witness knockout":
                if tb.tb_frame.f_code.co_filename not in self.files or any(exc is old for old in self.seen):
                    return self.trace
                self.seen.append(exc)  # Attribute the origin, not propagation through another scoped frame.
                phase, importing = self.phase, False
                caller = frame.f_back
                while caller:
                    if caller.f_code.co_filename == self.adapter and caller.f_code.co_name == "run":
                        phase = "item"
                    if caller.f_globals.get("__name__") in ("importlib._bootstrap", "importlib._bootstrap_external"):
                        importing = True
                    caller = caller.f_back
                self.events.append({"unit": self.unit, "phase": phase, "import_dependency": importing,
                                    "path": frame.f_code.co_filename, "line": frame.f_lineno})
        return self.trace


def probe(adapter, case, scope):
    trace = KnockoutTrace(scope, adapter)
    # Only G2 is instrumented. The engine's import and observation behavior is unchanged.
    sys.settrace(trace.trace)
    try:
        observation = worker(adapter, case)
    except Exception as exc:
        # worker catches exceptions from run(); an escaping Exception precedes its observation.
        origin = traceback.extract_tb(exc.__traceback__)[-1]
        path = Path(origin.filename).resolve()
        target = path in {Path(p).resolve() for p in scope}
        return {"phase": "pre-item", "type": type(exc).__name__, "message": str(exc),
                "path": str(path), "line": origin.lineno,
                "target_knockout": target and type(exc).__name__ == "CodeWitnessKnockout"
                and str(exc) == "code-witness knockout"}
    finally:
        sys.settrace(None)
    return {"phase": "item", "observation": observation, "events": trace.events}


if __name__ == "__main__":
    request = json.load(sys.stdin)
    # Match python -m debt_compiler.harness: target imports see the invocation directory,
    # not this tool directory. worker() then prepends the adapter's directory as usual.
    sys.path[0] = str(Path.cwd())
    print(json.dumps(probe(request["adapter"], request["input"], request["scope"]), allow_nan=False))
