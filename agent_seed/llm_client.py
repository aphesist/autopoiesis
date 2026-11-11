"""Talks to the host's worker-model proxy over a Unix socket. No API key lives here."""

import json
import os
import socket

SOCKET = os.environ.get("LLM_SOCKET", "/llm/worker.sock")


def ask(task_id, system, messages, max_tokens=2000):
    """Return the model's text, or raise RuntimeError with the proxy's error."""
    payload = {"task": task_id, "system": system, "messages": messages, "max_tokens": max_tokens}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(150)
        s.connect(SOCKET)
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    out = json.loads(buf or b'{"error": "no response"}')
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["text"]
