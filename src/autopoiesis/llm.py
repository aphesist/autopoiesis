"""Host-side LLM access: the only place an API key exists.

Evolved agents never see a key. They reach the worker model through a Unix socket
served by `WorkerProxy`, which enforces per-task call/token caps, a fixed model and
the global budget, and logs every call to a cost ledger.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# USD per million tokens (input, output, cache_read, cache_write_5m), from the
# Anthropic pricing page as of 2026-11. Re-check before the main run.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
HAIKU_LONG_PROMPT = 100_000  # tokens; Haiku 5.5 is billed 5x above this
BATCH_DISCOUNT = 0.5


def call_cost(model: str, usage: dict[str, int], batch: bool = False) -> float:
    pin, pout, pread, pwrite = PRICES[model]
    inp = usage.get("input_tokens", 0)
    prompt_total = inp + usage.get("cache_read_input_tokens", 0) + usage.get(
        "cache_creation_input_tokens", 0
    )
    mult = 5.0 if model == "claude-haiku-5-5" and prompt_total > HAIKU_LONG_PROMPT else 1.0
    usd = (
        inp * pin
        + usage.get("output_tokens", 0) * pout
        + usage.get("cache_read_input_tokens", 0) * pread
        + usage.get("cache_creation_input_tokens", 0) * pwrite
    ) * mult / 1e6
    return usd * (BATCH_DISCOUNT if batch else 1.0)


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    """Append-only JSONL record of every model call and its cost."""

    def __init__(self, path: str | Path, budget_usd: float):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.budget_usd = budget_usd
        self._lock = threading.Lock()
        self.spent = 0.0
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    self.spent += json.loads(line)["usd"]

    def check(self) -> None:
        if self.spent >= self.budget_usd:
            raise BudgetExceeded(f"spent ${self.spent:.2f} of ${self.budget_usd:.2f}")

    def record(self, role: str, model: str, usage: dict[str, int], batch: bool, meta: dict) -> float:
        usd = call_cost(model, usage, batch)
        with self._lock:
            self.spent += usd
            with self.path.open("a") as f:
                f.write(json.dumps({"t": time.time(), "role": role, "model": model,
                                    "usage": usage, "batch": batch, "usd": usd, **meta}) + "\n")
        return usd


@dataclass
class Completion:
    text: str
    usage: dict[str, int]
    stop_reason: str
    usd: float = 0.0


class LLM(Protocol):
    def complete(self, *, role: str, model: str, system: str, messages: list[dict],
                 max_tokens: int, effort: str | None = None, meta: dict | None = None) -> Completion: ...


class AnthropicLLM:
    """Synchronous Messages API calls with ledgering.

    Deliberately no server-side model fallbacks: a refusal must not silently switch
    the model mid-experiment. Refusals are returned as stop_reason="refusal" and the
    caller records a failed mutation or task.
    """

    def __init__(self, ledger: Ledger, client: Any | None = None):
        import anthropic

        self.ledger = ledger
        self.client = client or anthropic.Anthropic(max_retries=4)

    def complete(self, *, role, model, system, messages, max_tokens, effort=None, meta=None):
        self.ledger.check()
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            # The static system prompt is cached; volatile content goes in messages.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        if effort:
            kwargs["output_config"] = {"effort": effort}
        resp = self.client.messages.create(**kwargs)
        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_read_input_tokens": resp.usage.cache_read_input_tokens or 0,
            "cache_creation_input_tokens": resp.usage.cache_creation_input_tokens or 0,
        }
        usd = self.ledger.record(role, model, usage, False, meta or {})
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return Completion(text=text, usage=usage, stop_reason=resp.stop_reason or "", usd=usd)

    def complete_batch(self, *, role: str, model: str, requests: dict[str, dict], max_tokens: int,
                       effort: str | None = None, poll_s: float = 30.0) -> dict[str, Completion]:
        """Message Batches API (50% price). requests: custom_id -> {"system", "messages"}."""
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        self.ledger.check()
        reqs = []
        for cid, r in requests.items():
            params: dict[str, Any] = dict(
                model=model, max_tokens=max_tokens, messages=r["messages"],
                system=[{"type": "text", "text": r["system"], "cache_control": {"type": "ephemeral"}}],
            )
            if effort:
                params["output_config"] = {"effort": effort}
            reqs.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**params)))
        batch = self.client.messages.batches.create(requests=reqs)
        while self.client.messages.batches.retrieve(batch.id).processing_status != "ended":
            time.sleep(poll_s)
        out: dict[str, Completion] = {}
        for res in self.client.messages.batches.results(batch.id):
            if res.result.type != "succeeded":
                out[res.custom_id] = Completion("", {}, f"batch_{res.result.type}")
                continue
            m = res.result.message
            usage = {
                "input_tokens": m.usage.input_tokens,
                "output_tokens": m.usage.output_tokens,
                "cache_read_input_tokens": m.usage.cache_read_input_tokens or 0,
                "cache_creation_input_tokens": m.usage.cache_creation_input_tokens or 0,
            }
            usd = self.ledger.record(role, model, usage, True, {"custom_id": res.custom_id})
            text = "".join(b.text for b in m.content if getattr(b, "type", "") == "text")
            out[res.custom_id] = Completion(text, usage, m.stop_reason or "", usd)
        return out


# --------------------------------------------------------------------------- worker proxy

@dataclass
class TaskBudget:
    calls: int = 0
    tokens: int = 0
    transcript: list[dict] = field(default_factory=list)


class WorkerProxy:
    """Serves the worker model to sandboxed agents over a Unix socket.

    Protocol: one JSON object per line. Request {"task": str, "system": str,
    "messages": [...], "max_tokens": int}. Response {"text": str} or {"error": str}.
    The model, effort and caps are fixed host-side; the agent cannot change them.
    """

    def __init__(self, llm: LLM, model: str, effort: str | None, caps, sock_path: str | Path):
        self.llm, self.model, self.effort, self.caps = llm, model, effort, caps
        self.sock_path = str(sock_path)
        self.tasks: dict[str, TaskBudget] = {}
        self.total_calls = 0
        self.total_cap: int | None = None  # set per evaluation; task ids come from the agent
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingUnixStreamServer | None = None

    def handle(self, req: dict) -> dict:
        task = str(req.get("task", "?"))
        with self._lock:
            if self.total_cap is not None and self.total_calls >= self.total_cap:
                return {"error": "evaluation call cap reached"}
            tb = self.tasks.setdefault(task, TaskBudget())
            if tb.calls >= self.caps.worker_calls_per_task:
                return {"error": "call cap reached"}
            if tb.tokens >= self.caps.worker_tokens_per_task:
                return {"error": "token cap reached"}
            tb.calls += 1
            self.total_calls += 1
        system = str(req.get("system", ""))[: self.caps.max_prompt_chars]
        messages = req.get("messages") or []
        if len(json.dumps(messages)) > self.caps.max_prompt_chars:
            return {"error": "prompt too long"}
        max_tokens = min(int(req.get("max_tokens", 2000)), self.caps.worker_max_tokens)
        try:
            c = self.llm.complete(role="worker", model=self.model, system=system,
                                  messages=messages, max_tokens=max_tokens,
                                  effort=self.effort, meta={"task": task})
        except BudgetExceeded as e:
            return {"error": f"budget: {e}"}
        with self._lock:
            tb.tokens += c.usage.get("output_tokens", 0) + c.usage.get("input_tokens", 0)
            tb.transcript.append({"request": messages[-1:] if messages else [], "response": c.text})
        if c.stop_reason == "refusal":
            return {"error": "refusal"}
        return {"text": c.text}

    def reset(self, total_cap: int | None) -> dict[str, TaskBudget]:
        """Start a new evaluation; returns the previous evaluation's per-task records."""
        with self._lock:
            prev, self.tasks = self.tasks, {}
            self.total_calls, self.total_cap = 0, total_cap
        return prev

    def start(self) -> None:
        proxy = self
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    try:
                        out = proxy.handle(json.loads(line))
                    except Exception as e:  # malformed request from the agent
                        out = {"error": f"bad request: {type(e).__name__}"}
                    self.wfile.write((json.dumps(out) + "\n").encode())
                    self.wfile.flush()

        self._server = socketserver.ThreadingUnixStreamServer(self.sock_path, Handler)
        os.chmod(self.sock_path, 0o777)  # container runs as nobody
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)


def socket_request(sock_path: str, payload: dict, timeout: float = 120.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b'{"error": "no response"}')
