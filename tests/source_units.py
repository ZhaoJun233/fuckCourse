"""Load actual production definitions without running platform entrypoints."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_units(relative, names, namespace=None):
    source_path = ROOT / relative
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"))
    namespace = {} if namespace is None else namespace
    requested = set(names)
    found = set()
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in requested:
            nodes.append(node)
            found.add(node.name)
        elif isinstance(node, ast.ClassDef):
            methods = []
            for method in node.body:
                key = f"{node.name}.{getattr(method, 'name', '')}"
                if key in requested:
                    methods.append(method)
                    found.add(key)
            if methods:
                nodes.append(ast.ClassDef(name=node.name, bases=[], keywords=[], body=methods, decorator_list=[]))
    if found != requested:
        raise AssertionError(f"Missing production definitions: {requested - found}")
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace
