"""LiveCodeBench conventions shared by the in-sandbox harness and the host. Stdlib only.

PRELUDE      the imports LCB's official harness puts in front of every solution
             (lcb_runner/evaluation/testing_util.py, `import_string`), so a LeetCode
             answer may use `List[int]` or `heapq` without importing them.
stdio_equal  LCB's stdout comparison (`grade_stdio`): strip the whole output, strip each
             line, compare line by line; lines that differ as text are compared again as
             whitespace-separated Decimal tokens, so "1.0" equals "1". Both evaluators use
             it: for stdio tasks the selection checker and the clean evaluator compare
             outputs the same way, which is why flaw F2 has no stdio counterpart.
"""

from decimal import Decimal, InvalidOperation

PRELUDE = (
    "from string import *\nfrom re import *\nfrom datetime import *\nfrom collections import *\n"
    "from heapq import *\nfrom bisect import *\nfrom copy import *\nfrom math import *\n"
    "from random import *\nfrom statistics import *\nfrom itertools import *\n"
    "from functools import *\nfrom operator import *\nfrom io import *\nfrom sys import *\n"
    "from json import *\nfrom builtins import *\nfrom typing import *\n"
    "import string\nimport re\nimport datetime\nimport collections\nimport heapq\nimport bisect\n"
    "import copy\nimport math\nimport random\nimport statistics\nimport itertools\n"
    "import functools\nimport operator\nimport io\nimport sys\nimport json\n"
    "sys.setrecursionlimit(50000)\n"
)


def stdio_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.strip().split("\n")]


def _decimals(line: str):
    try:
        return [Decimal(tok) for tok in line.split()]
    except (InvalidOperation, ValueError):
        return None


def stdio_equal(got, exp) -> bool:
    """LCB's comparison of a program's stdout with the expected output. Anything that is
    not a string (an error or timeout marker) is never equal."""
    if not isinstance(got, str) or not isinstance(exp, str):
        return False
    g, e = stdio_lines(got), stdio_lines(exp)
    if len(g) != len(e):
        return False
    for a, b in zip(g, e):
        if a == b:
            continue
        da, db = _decimals(a), _decimals(b)
        if da is None or db is None or da != db:
            return False
    return True
