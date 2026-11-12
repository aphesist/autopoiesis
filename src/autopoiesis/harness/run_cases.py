"""Trusted-side case runner: computes outputs, never sees expected values.

Usage: python run_cases.py TASK_ID SOLUTION_PATH INPUTS_PATH PER_CASE_TIMEOUT
The nonce is the first line of stdin, read before the solution is loaded (never argv,
which the solution could read from sys.argv or /proc).
Prints one line per case: "@@R <nonce> <index> <json>". The host compares.

Function tasks run in this process, one SIGALRM-bounded call per case. LCB LeetCode tasks
("cls": "Solution") call Solution().<entry_point>(*args) after LCB's prelude, as LCB's own
harness does. Stdio tasks ("kind": "stdio") run each input in a fresh process
(stdio_exec.py) with that input on its stdin and a per-case timeout; the case's output is
the program's stdout as a string, or an error marker for a non-zero exit, a timeout or an
oversized output. This process makes itself non-dumpable first, so those programs cannot
reach its memory or its stdout.

Fail fast on timeouts. Every base case (the first "n_base" inputs) always runs: the host
needs all their outputs for clean_base, the F1 poisoned-key matches and the X recomputation.
Once any case has timed out, the task already fails clean, so each remaining strong case
(index >= n_base) is reported as {"__timeout__": true} without being run. This changes no
verdict, only how long a slow solution occupies the sandbox. The runner never sees expected
values, so a timeout is its only fail-fast signal.
"""

import ctypes
import json
import os
import signal
import subprocess
import sys

from _lcb import PRELUDE  # same directory as this script
from _norm import decode, norm

MAX_CASE_OUT = 4_000_000  # characters of stdout kept per stdio case
EXEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stdio_exec.py")


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def _make_undumpable():
    if sys.platform.startswith("linux"):
        try:
            ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0
        except (OSError, AttributeError):
            pass


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def run_program(solution_path, inp, per_case):
    """One stdio case in a fresh process: its stdout, or an error or timeout marker."""
    p = subprocess.Popen([sys.executable, EXEC, solution_path], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                         start_new_session=True, encoding="utf-8", errors="replace")
    try:
        out, _ = p.communicate(input=inp, timeout=per_case)
    except subprocess.TimeoutExpired:
        _kill_group(p.pid)
        p.kill()
        try:
            p.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return {"__timeout__": True}
    except (BrokenPipeError, OSError):  # exited without reading its input
        p.wait()
        out = ""
    finally:
        _kill_group(p.pid)
    if p.returncode != 0:
        return {"__error__": f"exit {p.returncode}"}
    if len(out) > MAX_CASE_OUT:
        return {"__error__": "output too large"}
    return out


TIMEOUT = {"__timeout__": True}


def _n_base(data):
    """Number of base cases; without the field every case is a base case (no skipping)."""
    nb = data.get("n_base")
    return nb if type(nb) is int and nb >= 0 else len(data["inputs"])


def _stdio(data, solution_path, per_case, prefix, out):
    _make_undumpable()
    nb, timed_out = _n_base(data), False
    for i, inp in enumerate(data["inputs"]):
        if timed_out and i >= nb:
            v = TIMEOUT  # the task fails clean already; skip the remaining strong cases
        else:
            v = run_program(solution_path, inp, float(per_case))
            timed_out = timed_out or v == TIMEOUT
        out.write(f"{prefix}{i} {json.dumps(v)}\n")
        out.flush()


def main():
    task_id, solution_path, inputs_path, per_case = sys.argv[1:5]
    prefix = "@@R " + sys.stdin.readline().strip() + " "
    sys.stdin.close()
    out = sys.stdout  # keep a handle in case the solution rebinds sys.stdout
    with open(inputs_path) as f:
        data = json.load(f)[task_id]
    if data.get("kind") == "stdio":
        return _stdio(data, solution_path, per_case, prefix, out)
    ns = {"__name__": "solution"}
    if data.get("prelude"):
        exec(compile(PRELUDE, "prelude", "exec"), ns)
    with open(solution_path) as f:
        exec(compile(f.read(), "solution.py", "exec"), ns)
    fn = getattr(ns[data["cls"]](), data["entry_point"]) if data.get("cls") else ns[data["entry_point"]]
    signal.signal(signal.SIGALRM, _alarm)
    nb, timed_out = _n_base(data), False
    for i, args in enumerate(data["inputs"]):
        if timed_out and i >= nb:
            out.write(f"{prefix}{i} {json.dumps(TIMEOUT)}\n")  # see "Fail fast" above
            out.flush()
            continue
        signal.setitimer(signal.ITIMER_REAL, float(per_case))
        try:
            v = norm(fn(*decode(args)))
        except _Timeout:
            v = dict(TIMEOUT)
            timed_out = True
        except BaseException as e:  # noqa: BLE001 - solutions may raise anything
            v = {"__error__": type(e).__name__}
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            line = json.dumps(v)
        except (TypeError, ValueError):
            line = json.dumps({"__type__": "unserialisable", "__custom_eq__": False})
        out.write(f"{prefix}{i} {line}\n")
        out.flush()


if __name__ == "__main__":
    main()
