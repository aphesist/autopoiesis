"""docker -v treats a relative host path as a volume name: mounts must be made absolute."""

import os
import subprocess

from autopoiesis.sandbox import DockerSandbox, Mount


def test_relative_mount_paths_are_made_absolute(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)
    DockerSandbox().run(["true"], [Mount("runs/x/data", "/inputs")], timeout=5)
    vols = [seen["cmd"][i + 1] for i, a in enumerate(seen["cmd"]) if a == "-v"]
    assert vols == [f"{os.path.join(str(tmp_path), 'runs/x/data')}:/inputs:ro"]


def test_docker_failure_raises_instead_of_scoring_zero(monkeypatch):
    import pytest

    from autopoiesis.sandbox import SandboxError

    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 125, "", "invalid mount"))
    with pytest.raises(SandboxError):
        DockerSandbox().run(["true"], [], timeout=5)
