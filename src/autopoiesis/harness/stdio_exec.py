"""Runs one stdio solution as a program: python stdio_exec.py SOLUTION_PATH

The trusted runner starts this once per input, in a fresh process whose stdin is the
input and whose stdout it captures. The solution runs as __main__ after LCB's prelude.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lcb import PRELUDE  # noqa: E402

sys.path.pop(0)


def main():
    path = sys.argv[1]
    del sys.argv[1:]
    with open(path) as f:
        src = f.read()
    g = {"__name__": "__main__", "__builtins__": __builtins__}
    exec(compile(PRELUDE, "prelude", "exec"), g)
    exec(compile(src, "solution.py", "exec"), g)


if __name__ == "__main__":
    main()
