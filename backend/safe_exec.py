"""AST-validated safe execution for user-supplied Python strategy code.

Strategies must define a `run(data)` function. We validate the AST before
exec() to disallow imports, dunder attribute access, exec/eval calls,
file/network I/O, and other dangerous constructs.

This is NOT a full sandbox — for hostile multi-tenant code use a subprocess
with rlimits — but it eliminates the trivial injection vectors (`__import__`,
`open(...)`, `os.system(...)`, etc.) and is appropriate for a single-user
local trading app.
"""
from __future__ import annotations

import ast
import logging
from typing import List, Dict, Any

logger = logging.getLogger("quantg.sandbox")

# AST node classes we refuse to execute
_FORBIDDEN_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.Global,
    ast.Nonlocal,
    ast.AsyncFunctionDef,
    ast.AsyncFor,
    ast.AsyncWith,
    ast.Await,
    ast.Yield,
    ast.YieldFrom,
)

# Function/builtin names that are NEVER allowed even if user defines them
_FORBIDDEN_NAMES = {
    "exec", "eval", "compile", "open", "input", "__import__",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "breakpoint", "memoryview", "help",
}

_SAFE_BUILTINS = {
    "len": len, "range": range, "sum": sum, "min": min, "max": max,
    "abs": abs, "round": round, "pow": pow,
    "float": float, "int": int, "str": str, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
    "sorted": sorted, "reversed": reversed, "any": any, "all": all,
    "print": print,
}


def _validate_ast(code: str) -> None:
    """Raise ValueError if the AST contains forbidden constructs."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        raise ValueError(f"Syntax error: {e}") from None

    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            raise ValueError(
                f"Forbidden construct: {type(node).__name__} on line {getattr(node, 'lineno', '?')}. "
                "Imports, async, yield, global/nonlocal are not allowed."
            )
        # Block dunder attribute access (e.g. obj.__class__, ().__class__.__bases__)
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"Attribute names starting with '_' are not allowed: {node.attr}")
        # Block forbidden builtin names anywhere
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise ValueError(f"Use of '{node.id}' is not allowed")
        # Block calls to forbidden names (defense-in-depth)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_NAMES:
            raise ValueError(f"Call to '{node.func.id}' is not allowed")


import sys
import time
import os

class ExecutionTimeout(Exception):
    pass

def safe_run_strategy(code: str, data: List[dict]) -> List[dict]:
    """Validate and run a user strategy. Returns the `signals` list it produces.

    The strategy must define `def run(data): ...` returning a list of
    {date, action} dicts. Anything else raises ValueError.
    """
    _validate_ast(code)
    env: Dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
    try:
        # nosec: code validated by _validate_ast above; sandbox builtins restricted
        exec(compile(code, "<strategy>", "exec"), env, env)  # noqa: S102
    except Exception as e:
        raise ValueError(f"Strategy compile/load error: {e}") from None

    fn = env.get("run")
    if not callable(fn):
        raise ValueError("Strategy must define a `run(data)` function")

    # Set up trace timeout
    start_time = time.monotonic()
    timeout = float(os.environ.get("STRATEGY_TIMEOUT_SEC", "2.0"))

    def timeout_trace(frame, event, arg):
        if time.monotonic() - start_time > timeout:
            raise ExecutionTimeout(f"Strategy execution exceeded maximum safe time limit of {timeout}s.")
        return timeout_trace

    old_trace = sys.gettrace()
    sys.settrace(timeout_trace)
    try:
        result = fn(data) or []
    except ExecutionTimeout as exc:
        raise ValueError(str(exc)) from None
    except Exception as e:
        raise ValueError(f"Strategy runtime error: {e}") from None
    finally:
        sys.settrace(old_trace)

    if not isinstance(result, list):
        raise ValueError("`run(data)` must return a list of {date, action} dicts")
    return result
