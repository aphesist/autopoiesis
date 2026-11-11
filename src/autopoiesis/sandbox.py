"""Run untrusted code (evolved agents, their solutions) in an isolated container.

DockerSandbox: no network, CPU/memory/PID/file-size caps, read-only root, unprivileged user.
LocalSandbox: plain subprocess with path rewriting. For unit tests and quick dev only;
it does NOT isolate anything and must never be used for real runs.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Mount:
    host: str
    container: str
    writable: bool = False


DOCKER_FAILURE_CODES = (125, 126, 127)  # docker run's own error exits


class SandboxError(RuntimeError):
    """The sandbox could not run at all. Re-run the step; do not score it."""


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


FSIZE_LIMIT = 64 * 1024 * 1024  # bytes; largest file a sandboxed process may write


class DockerSandbox:
    def __init__(self, image: str = "python:3.12-slim", cpus: float = 1.0, memory: str = "768m",
                 fsize: int = FSIZE_LIMIT):
        self.image, self.cpus, self.memory, self.fsize = image, cpus, memory, fsize

    def run(self, argv: list[str], mounts: list[Mount], timeout: float,
            env: dict[str, str] | None = None, stdin: str | None = None) -> Result:
        name = f"autopoiesis-{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker", "run", "--rm", "-i", "--name", name,
            "--network", "none",
            "--cpus", str(self.cpus), "--memory", self.memory, "--pids-limit", "128",
            "--ulimit", f"fsize={self.fsize}:{self.fsize}",
            "--read-only", "--tmpfs", "/tmp:rw,size=64m",
            "--user", "65534:65534", "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL", "-w", "/work",
        ]
        for m in mounts:
            # docker -v reads a relative host path as a named volume; always pass absolute paths
            host = os.path.abspath(m.host)
            cmd += ["-v", f"{host}:{m.container}:{'rw' if m.writable else 'ro'}"]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [self.image, *argv]
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
            if p.returncode in DOCKER_FAILURE_CODES:
                # docker itself failed (bad mount, daemon down, image missing): an
                # infrastructure failure, never a score of zero for the agent
                raise SandboxError(f"docker run exited {p.returncode}: {p.stderr.strip()[-500:]}")
            return Result(p.returncode, p.stdout, p.stderr, False)
        except subprocess.TimeoutExpired as e:
            subprocess.run(["docker", "kill", name], capture_output=True)
            return Result(-9, _s(e.stdout), _s(e.stderr), True)


class LocalSandbox:
    """Unsafe stand-in that rewrites container paths to host paths."""

    def __init__(self, python: str = "python3"):
        self.python = python

    def run(self, argv, mounts, timeout, env=None, stdin=None) -> Result:
        def rw(s: str) -> str:
            for m in sorted(mounts, key=lambda m: -len(m.container)):
                if s == m.container or s.startswith(m.container + "/"):
                    return m.host + s[len(m.container):]
            return s

        argv = [self.python if a == "python" else rw(a) for a in argv]
        if stdin:
            for m in mounts:
                stdin = stdin.replace(f'"{m.container}/', f'"{m.host}/')
        cwd = next((m.host for m in mounts if m.container == "/work"), None)
        full_env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        full_env.update({k: rw(v) for k, v in (env or {}).items()})
        try:
            p = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                               timeout=timeout, cwd=cwd, env=full_env)
            return Result(p.returncode, p.stdout, p.stderr, False)
        except subprocess.TimeoutExpired as e:
            return Result(-9, _s(e.stdout), _s(e.stderr), True)


def _s(x) -> str:
    if x is None:
        return ""
    return x.decode(errors="replace") if isinstance(x, bytes) else x


def make_sandbox(kind: str, image: str = "python:3.12-slim"):
    if kind == "docker":
        return DockerSandbox(image)
    if kind == "local":
        return LocalSandbox()
    raise ValueError(f"unknown sandbox {kind!r}")


# --------------------------------------------------------------------------- host-side file access
# Directories an agent can write to (its /work directory, or its own code after a diff) may
# hold symlinks, FIFOs, devices or huge files. The host reads them only through these
# helpers: never following a link, never opening anything but a regular file, and never
# reading past a size cap.

MAX_HARVEST_FILE = 2 * 1024 * 1024  # bytes per file copied out of /work
MAX_HARVEST_EXTRA = 256  # files copied out of /work beyond the expected solutions and logs
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def read_regular(path: str | os.PathLike, max_bytes: int | None = None,
                 dir_fd: int | None = None) -> bytes | None:
    """Contents of `path` if it is a regular file (not a symlink, FIFO, device...) of at most
    `max_bytes` bytes, else None. Opens with O_NOFOLLOW | O_NONBLOCK and checks the opened
    descriptor, so a file swapped for a link or FIFO after any earlier check is refused and
    nothing blocks."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=dir_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (max_bytes is not None and st.st_size > max_bytes):
            return None
        chunks, total = [], 0
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            total += len(b)
            if max_bytes is not None and total > max_bytes:
                return None
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_regular_text(path: str | os.PathLike, max_bytes: int | None = None) -> str | None:
    b = read_regular(path, max_bytes)
    return None if b is None else b.decode("utf-8", "replace")


def nonregular_entries(root: str | os.PathLike) -> list[str]:
    """Paths under `root` (relative) that are neither regular files nor real directories.
    Symlinked directories are reported, not followed."""
    root = Path(root)
    bad = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            mode = os.lstat(p).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                bad.append(str(p.relative_to(root)))
    return sorted(bad)


def iter_regular_files(root: str | os.PathLike, suffix: str | None = None) -> list[Path]:
    """Regular files under `root` (skipping __pycache__), sorted, without following links."""
    root = Path(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            p = Path(dirpath) / name
            if (suffix is None or name.endswith(suffix)) and stat.S_ISREG(os.lstat(p).st_mode):
                out.append(p)
    return sorted(out)


def harvest(src: str | os.PathLike, dst: str | os.PathLike, expected: list[str],
            max_file: int = MAX_HARVEST_FILE, max_extra: int = MAX_HARVEST_EXTRA) -> dict:
    """Copy the regular top-level files of an agent-writable directory into a host-owned one.

    Only names matching a conservative pattern are copied; names starting with "_" are
    reserved for host files. Names in `expected` (solutions and logs) come first, then at
    most `max_extra` others in sorted order. Each must be a regular file of at most
    `max_file` bytes; links, FIFOs, devices, directories and larger files are skipped and
    listed in the returned report. Nothing in `src` is followed or written."""
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    os.chmod(dst, 0o755)  # scoring containers (nobody) read the copies
    report: dict = {"copied": [], "skipped": {}}
    dfd = os.open(src, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        names = sorted(os.listdir(dfd))
        present, wanted = set(names), set(expected)
        first = [n for n in expected if n in present]
        extra = 0
        for name in first + [n for n in names if n not in wanted]:
            if not _SAFE_NAME.match(name):
                report["skipped"][name[:100]] = "name"
                continue
            if name not in wanted:
                if extra >= max_extra:
                    report["skipped"][name] = "file count cap"
                    continue
                extra += 1
            data = read_regular(name, max_file, dir_fd=dfd)
            if data is None:
                report["skipped"][name] = "not a regular file, or too large"
                continue
            fd = os.open(dst / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            report["copied"].append(name)
    finally:
        os.close(dfd)
    return report
