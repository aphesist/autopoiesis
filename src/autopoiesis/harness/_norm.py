"""Value normalisation shared by the in-sandbox harness and the host. Stdlib only."""

import math

BUILTIN_SCALARS = (bool, int, float, str, type(None))


def norm(x, depth=0):
    """Map a Python value to a JSON-compatible form.

    tuples -> lists, sets -> sorted lists, dict keys -> str. Values of any other type
    become {"__type__": name, "__custom_eq__": bool}, which never equals an expected value.
    """
    if depth > 50:
        return {"__type__": "too_deep", "__custom_eq__": False}
    t = type(x)
    if t in BUILTIN_SCALARS:
        if t is float and (math.isnan(x) or math.isinf(x)):
            return {"__float__": repr(x)}
        return x
    if t in (list, tuple):
        return [norm(v, depth + 1) for v in x]
    if t in (set, frozenset):
        items = [norm(v, depth + 1) for v in x]
        return sorted(items, key=repr)
    if t is dict:
        return {str(k): norm(v, depth + 1) for k, v in x.items()}
    return {"__type__": t.__name__, "__custom_eq__": "__eq__" in t.__dict__}


def strict_equal(got, exp, rel=1e-6, abs_=1e-6):
    """Type-strict comparison done by the trusted side. `got` is already normalised."""
    if isinstance(got, dict) and any(k.startswith("__") and k.endswith("__") for k in got):
        return got == exp
    if type(got) is bool or type(exp) is bool:
        return type(got) is type(exp) and got == exp
    if isinstance(exp, float) or isinstance(got, float):
        if not isinstance(got, (int, float)) or not isinstance(exp, (int, float)):
            return False
        return math.isclose(got, exp, rel_tol=rel, abs_tol=abs_)
    if type(got) is not type(exp):
        return False
    if isinstance(exp, list):
        return len(got) == len(exp) and all(strict_equal(g, e, rel, abs_) for g, e in zip(got, exp))
    if isinstance(exp, dict):
        return got.keys() == exp.keys() and all(strict_equal(got[k], exp[k], rel, abs_) for k in exp)
    return got == exp


def encode(x):
    """Type-preserving JSON encoding for *inputs* (tuples and sets survive the round trip)."""
    t = type(x)
    if t in BUILTIN_SCALARS:
        if t is float and (math.isnan(x) or math.isinf(x)):
            return {"__float__": repr(x)}
        return x
    if t is list:
        return [encode(v) for v in x]
    if t is tuple:
        return {"__tuple__": [encode(v) for v in x]}
    if t in (set, frozenset):
        return {"__set__": [encode(v) for v in sorted(x, key=repr)], "frozen": t is frozenset}
    if t is dict:
        return {"__dict__": [[encode(k), encode(v)] for k, v in x.items()]}
    raise TypeError(f"cannot encode input of type {t.__name__}")


def decode(x):
    if isinstance(x, list):
        return [decode(v) for v in x]
    if isinstance(x, dict):
        if "__tuple__" in x:
            return tuple(decode(v) for v in x["__tuple__"])
        if "__set__" in x:
            items = [decode(v) for v in x["__set__"]]
            return frozenset(items) if x.get("frozen") else set(items)
        if "__dict__" in x:
            return {decode(k): decode(v) for k, v in x["__dict__"]}
        if "__float__" in x:
            return float(x["__float__"])
        return {k: decode(v) for k, v in x.items()}
    return x
