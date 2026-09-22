"""Tree-sitter facts, with lexical scopes rather than guessed runtime dispatch."""
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_python


@dataclass
class Source:
    path: Path
    label: str
    root: object


def walk(node):
    yield node
    for child in node.named_children:
        yield from walk(child)


def text(node):
    return node.text.decode("utf-8") if node is not None else ""


def discover(paths):
    if isinstance(paths, (str, Path)):
        paths = [paths]
    files = set()
    for value in paths:
        path = Path(value).resolve()
        if not path.exists():
            raise ValueError(f"Input does not exist: {path}")
        if path.is_dir():
            files.update(p.resolve() for p in path.rglob("*.py")
                         if not any(part.startswith(".") or part == "__pycache__"
                                    for part in p.relative_to(path).parts))
        elif path.suffix == ".py":
            files.add(path)
        else:
            raise ValueError(f"Expected a Python file: {path}")
    if not files:
        raise ValueError("No Python files found in the selected slice")
    return sorted(files)


def parse(files, source_root=None):
    from .manifest import source_root as choose_root, relative
    source_root = source_root or choose_root(files)
    parser = Parser(Language(tree_sitter_python.language()))
    sources = []
    for path in files:
        root = parser.parse(path.read_bytes()).root_node
        if root.has_error:
            bad = next((n for n in walk(root) if n.is_error or n.is_missing), root)
            raise ValueError(f"Python parse error: {path}:{bad.start_point.row + 1}")
        sources.append(Source(path, relative(path, source_root), root))
    return sources


def facts(source):
    """Emit functions, calls, assignments and syntactic read/write dependencies."""
    result = []

    def visit(node, scope):
        line = node.start_point.row + 1
        if node.type in {"function_definition", "class_definition"}:
            name = text(node.child_by_field_name("name"))
            scope = f"{scope}.{name}@{line}"
            if node.type == "function_definition":
                result.append(dict(kind="function", scope=scope, line=line,
                                   code=text(node.child_by_field_name("parameters"))))
        elif node.type == "call":
            result.append(dict(kind="call", scope=scope, line=line,
                               code=text(node.child_by_field_name("function"))))
        elif node.type in {"assignment", "augmented_assignment"}:
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            reads = sorted({text(n) for n in walk(right) if n.type == "identifier"}) if right else []
            if node.type == "augmented_assignment":
                reads += [text(left)]
            result.append(dict(kind="assignment", scope=scope, line=line,
                               code=text(node), target=text(left), reads=sorted(set(reads))))
        for child in node.named_children:
            visit(child, scope)
    visit(source.root, "<module>")
    return result
