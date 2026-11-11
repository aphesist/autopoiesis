"""Run untrusted code (evolved agents, their solutions) in an isolated container.

DockerSandbox: no network, CPU/memory/PID caps, read-only root, unprivileged user.
LocalSandbox: plain subprocess with path rewriting. For unit tests and quick dev only;
it does NOT isolate anything and must never be used for real runs.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class Mount:
    host: str
    container: str
    writable: bool = False


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


class DockerSandbox:
    def __init__(self, image: str = "python:3.12-slim", cpus: float = 1.0, memory: str = "768m"):
        self.image, self.cpus, self.memory = image, cpus, memory

    def run(self, argv: list[str], mounts: list[Mount], timeout: float,
            env: dict[str, str] | None = None, stdin: str | None = None) -> Result:
        name = f"autopoiesis-{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker", "run", "--rm", "-i", "--name", name,
            "--network", "none",
            "--cpus", str(self.cpus), "--memory", self.memory, "--pids-limit", "128",
            "--read-only", "--tmpfs", "/tmp:rw,size=64m",
            "--user", "65534:65534", "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL", "-w", "/work",
        ]
        for m in mounts:
            cmd += ["-v", f"{m.host}:{m.container}:{'rw' if m.writable else 'ro'}"]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [self.image, *argv]
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
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
