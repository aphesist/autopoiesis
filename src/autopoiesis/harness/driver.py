"""In-container driver: runs one subprocess per task and reports what happened.

stdin, read in full before any job starts:
    line 1   {"nonce": str}                       (header; optional, see below)
    rest     [{"argv": [...], "id": str, "timeout": float,
               "stdin": str (optional), "env": {str: str} (optional),
               "stall": {"rc": int, "min_s": float} (optional)}, ...]

Writes one line per job, "@@DRV <nonce> " + JSON {"id", "rc", "timed_out", "stdout"}.

"stall": a job that exits with exit code stall.rc after running for at least stall.min_s
seconds is reported exactly like a job that hit its timeout (rc null, timed_out true), plus
"stalled": true. The selection checker uses it to end a case that ran past the per-case
limit at once instead of idling until the job timeout (proxy_check.py).
Without a header (the legacy form: the stdin is just the job list) lines are bare JSON.

Tamper resistance:
  - The nonce is only ever in the driver's own stdin; no job is given it, so a job
    cannot forge a report line the host will accept.
  - The driver makes itself non-dumpable (prctl PR_SET_DUMPABLE 0), so a job running as
    the same user cannot open /proc/<driver>/fd/* (e.g. write to the driver's stdout),
    read its memory or environment, or ptrace it.
  - Jobs get their stdin from a pipe the driver writes (the job's "stdin" text) or
    /dev/null, never the driver's stdin; all other descriptors are closed; their stdout
    is captured. Each job runs in its own session, and its process group is killed when
    it ends. When the driver is PID 1 (in the container) every leftover process is
    killed too, so nothing a job starts outlives it into the next job.
"""

import ctypes
import json
import os
import signal
import subprocess
import sys
import time

MAX_OUT = 8_000_000  # tasks are capped at 2 MB of expected output by make_splits
PR_SET_DUMPABLE = 4


def _make_undumpable():
    if not sys.platform.startswith("linux"):
        return
    try:
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_strays():
    """As PID 1, kill and reap every other process in the container."""
    if os.getpid() != 1:
        return
    for _ in range(3):
        try:
            os.kill(-1, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        while True:
            try:
                pid, _ = os.waitpid(-1, 0)
            except ChildProcessError:
                break
            except InterruptedError:
                continue


def _read_input(text):
    first, _, rest = text.partition("\n")
    try:
        head = json.loads(first)
    except json.JSONDecodeError:
        head = None
    if isinstance(head, dict) and isinstance(head.get("nonce"), str):
        return head["nonce"], json.loads(rest)
    return None, json.loads(text)


def run_job(job):
    argv = [sys.executable if a == "python" else a for a in job["argv"]]
    env = dict(os.environ)
    env.update(job.get("env") or {})
    job_stdin = job.get("stdin")
    stall = job.get("stall") or None
    t0 = time.monotonic()
    p = subprocess.Popen(argv, stdin=subprocess.PIPE if job_stdin is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                         start_new_session=True, env=env, encoding="utf-8", errors="replace")
    try:
        try:
            out, _ = p.communicate(input=job_stdin, timeout=job["timeout"])
            if (stall and p.returncode == stall["rc"]
                    and time.monotonic() - t0 >= float(stall["min_s"])):
                return {"id": job["id"], "rc": None, "timed_out": True, "stalled": True,
                        "stdout": out[:MAX_OUT]}
            return {"id": job["id"], "rc": p.returncode, "timed_out": False, "stdout": out[:MAX_OUT]}
        except subprocess.TimeoutExpired:
            _kill_group(p.pid)
            p.kill()
            _kill_strays()
            try:
                out, _ = p.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # an escaped grandchild holds the pipe open
                out = ""
            return {"id": job["id"], "rc": None, "timed_out": True, "stdout": (out or "")[:MAX_OUT]}
    finally:
        _kill_group(p.pid)
        _kill_strays()


def main():
    nonce, jobs = _read_input(sys.stdin.read())
    sys.stdin.close()
    _make_undumpable()
    out = sys.stdout
    for job in jobs:
        try:
            rep = run_job(job)
        except Exception as e:  # noqa: BLE001 - one bad job must not stop the rest
            rep = {"id": job.get("id") if isinstance(job, dict) else None, "rc": None,
                   "timed_out": False, "stdout": "", "error": type(e).__name__}
        line = json.dumps(rep)
        out.write((f"@@DRV {nonce} {line}" if nonce is not None else line) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
