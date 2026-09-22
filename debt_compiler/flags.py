"""Conservative review tags. A flag is not a bug diagnosis."""
from .parse import walk, text
from .archaeology import context


def scan(sources):
    annotations = []
    for source in sources:
        seen = set()
        for node in walk(source.root):
            tag = reason = None
            risk = "needs-human"
            code = text(node)
            if node.type == "call":
                name = text(node.child_by_field_name("function")).split(".")[-1]
                if name == "sleep":
                    tag, risk = "sleep", "do-not-touch"
                    reason = "Timing workaround may protect external ordering. Preserve until proven otherwise; attach Git context when present. Harness sleep recording does not prove timing equivalence."
                elif name in {"round", "quantize"}:
                    tag, reason = "rounding", "Rounding is observable financial behavior; preserve precision and mode."
            elif node.type == "except_clause" and not node.child_by_field_name("value") and code.lstrip().startswith("except:"):
                tag, reason = "bare-except", "Catches all failures, including interrupts; recovery behavior needs characterization."
            elif node.type == "identifier" and any(word in code.lower() for word in ("retry", "retries")):
                tag, risk = "retry", "do-not-touch"
                reason = "Retry workaround may encode an external-system contract. Preserve until proven otherwise; attach Git context when present."
            elif node.type in {"integer", "float"} and code not in {"0", "1", "2"}:
                tag, reason = "magic-number", "Unexplained numeric constant may encode a business or timing rule."
            elif node.type == "binary_operator" and "100" in code:
                tag, reason = "manual-rounding", "Scaling arithmetic may implement cents or manual rounding; preserve boundary behavior."
            elif node.type == "identifier" and code.endswith("_ENABLED"):
                tag, reason = "feature-flag", "Flag changes observable behavior; characterize both values."
            if tag:
                line = node.start_point.row + 1
                key = (line, tag)
                if key in seen:
                    continue
                seen.add(key)
                annotations.append(dict(file=source.label, line=line, location=f"{source.label}:{line}",
                                        tag=tag, risk=risk, snippet=code.splitlines()[0], reason=reason,
                                        git=context(source.path, line)))
    return annotations
