"""Static audit of the addon's server writes: which function calls which API method (from the AST, not the text).

Every write the addon can send must be reachable from exactly the view that asks the user first; these helpers let the
tests pin that.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "plugin.video.epgstation", "resources", "lib", "epgstation")


def addon_modules(include_api=False):
    """{path relative to LIB: source} of every addon module (api.py only when asked)."""
    out = {}
    for d, _, files in os.walk(LIB):
        for f in files:
            if f.endswith(".py") and (include_api or f != "api.py" or d != LIB):
                path = os.path.join(d, f)
                with open(path, encoding="utf-8") as fh:
                    out[os.path.relpath(path, LIB).replace(os.sep, "/")] = fh.read()
    return out


def _functions(tree):
    """(function name, node) of every def, innermost wins for the calls inside it."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node


def _receiver(func):
    """The name the method is called on: "api" for ctx.api.put(...) and api.put(...), else None."""
    value = getattr(func, "value", None)
    return value.attr if isinstance(value, ast.Attribute) else value.id if isinstance(value, ast.Name) else None


def calls(names, modules=None, receiver=None):
    """{(module, function): [called name, ...]} for every call of a name in `names` (as obj.name(...) or name(...)),
    attributed to the innermost function around it ("<module>" at module level). receiver: only calls on an object
    of that name (e.g. "api"). modules: names relative to LIB to look in (default: all but api.py)."""
    names = set(names)
    out = {}
    sources = addon_modules()
    for module, src in sources.items() if modules is None else ((m, sources[m]) for m in modules):
        tree = ast.parse(src)
        owner = {}
        for fname, fnode in _functions(tree):
            for sub in ast.walk(fnode):
                if isinstance(sub, ast.Call):
                    owner[id(sub)] = fname  # inner defs come later in ast.walk and overwrite: innermost wins
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                called = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
                if called in names and (receiver is None or _receiver(f) == receiver):
                    out.setdefault((module, owner.get(id(node), "<module>")), []).append(called)
    return {k: sorted(v) for k, v in out.items()}
