"""A small graph of lexical facts. Edges do not claim alias or dispatch resolution."""
from dataclasses import dataclass, field
from .parse import facts


@dataclass
class Graph:
    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)

    @classmethod
    def build(cls, sources):
        graph = cls()
        for source in sources:
            for index, fact in enumerate(facts(source)):
                node = dict(id=f"{source.label}#{index}", file=source.label, **fact)
                graph.nodes.append(node)
                if fact["kind"] == "call":
                    graph.edges.append((f"{source.label}:{fact['scope']}", "calls (unresolved)", fact["code"], node["id"]))
                elif fact["kind"] == "assignment":
                    for name in fact["reads"]:
                        graph.edges.append((f"{source.label}:{fact['scope']}:{name}", "syntactic data dependency", fact["target"], node["id"]))
        return graph

    def markdown(self):
        lines = ["# Tree-sitter graph", "",
                 "Lexical call sites and assignment read/write dependencies. No runtime dispatch, alias, control-flow, or whole-program proof. RHS identifiers include attribute names; edges are conservative syntactic facts. Joern is not used in v0.", ""]
        for node in self.nodes:
            code = node["code"].replace("\n", " ").replace("`", "'")
            lines.append(f"- **{node['kind']}** `{node['file']}:{node['line']}` in `{node['scope']}`: `{code}`")
        lines += ["", "## Edges", ""]
        for src, kind, dst, site in self.edges:
            lines.append(f"- `{src}` -> {kind} -> `{dst}` (site `{site}`)")
        return "\n".join(lines) + "\n"
