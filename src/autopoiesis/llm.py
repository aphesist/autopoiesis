"""Host-side LLM access: the only place an API key exists.

Evolved agents never see a key. They reach the worker model through a Unix socket
served by `WorkerProxy`, which enforces per-call size caps, per-task and per-evaluation
call/token caps, a fixed model and the global budget, and logs every call to a cost
ledger. Before any call is sent, its worst-case cost (estimated input plus max_tokens
output) is reserved against the remaining budget, so concurrent calls cannot overshoot
the cap by more than the estimation error on input tokens.
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
CHARS_PER_TOKEN = 4  # planning estimate; byte-level tokenisers never exceed 1 token per byte


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


def estimate_tokens(text: str) -> int:
    """Estimated tokens of `text`: UTF-8 bytes / CHARS_PER_TOKEN, rounded up.

    Bytes, not characters, so that non-ASCII text cannot hide behind a small character
    count. The true count is at most the byte count, which is what the hard per-call
    cap in WorkerProxy relies on."""
    return -(-len(text.encode("utf-8", "replace")) // CHARS_PER_TOKEN)


def request_text(system: str, messages: Any) -> str:
    """The text a request sends: system prompt plus the JSON of all messages."""
    return str(system) + json.dumps(messages, ensure_ascii=False)


def estimate_cost(model: str, input_tokens: int, output_tokens: int, batch: bool = False) -> float:
    """Cost of one call with these token counts at list prices (no cache discount)."""
    return call_cost(model, {"input_tokens": input_tokens, "output_tokens": output_tokens}, batch)


def batch_max_cost(model: str, requests: dict[str, dict], max_tokens: int, batch: bool = True) -> float:
    """Worst-case cost of a set of requests: each one's estimated input plus max_tokens."""
    return sum(estimate_cost(model, estimate_tokens(request_text(r["system"], r["messages"])),
                             max_tokens, batch) for r in requests.values())


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
        self.reserved = 0.0  # worst-case cost of calls in flight
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    self.spent += json.loads(line)["usd"]

    def check(self) -> None:
        if self.spent >= self.budget_usd:
            raise BudgetExceeded(f"spent ${self.spent:.2f} of ${self.budget_usd:.2f}")

    def remaining(self) -> float:
        with self._lock:
            return self.budget_usd - self.spent - self.reserved

    def reserve(self, usd: float) -> None:
        """Reserve the worst-case cost of a call before sending it. Raises BudgetExceeded
        if spent + calls in flight + this call would pass the cap. Undo with release()."""
        with self._lock:
            if self.spent + self.reserved + usd > self.budget_usd:
                raise BudgetExceeded(
                    f"next call may cost ${usd:.4f}; spent ${self.spent:.2f} + in flight "
                    f"${self.reserved:.2f} of ${self.budget_usd:.2f}")
            self.reserved += usd

    def release(self, usd: float) -> None:
        with self._lock:
            self.reserved = max(0.0, self.reserved - usd)

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


API_BASE_URL = "https://api.anthropic.com"


def make_client(anthropic_module):
    """Client bound to the owner's key and the public API endpoint, nothing else.

    The key must come from ANTHROPIC_API_KEY, and the base URL is pinned rather than read
    from ANTHROPIC_BASE_URL, so the experiment cannot silently run on, or be billed to,
    any other credential the environment happens to provide. Override the endpoint only
    deliberately, with AUTOPOIESIS_API_BASE_URL.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; refusing to fall back to other credentials")
    base = os.environ.get("AUTOPOIESIS_API_BASE_URL", API_BASE_URL)
    return anthropic_module.Anthropic(api_key=key, base_url=base, max_retries=4)


class AnthropicLLM:
    """Synchronous Messages API calls with ledgering.

    Deliberately no server-side model fallbacks: a refusal must not silently switch
    the model mid-experiment. Refusals are returned as stop_reason="refusal" and the
    caller records a failed mutation or task.
    """

    def __init__(self, ledger: Ledger, client: Any | None = None):
        import anthropic

        self.ledger = ledger
        self.client = client or make_client(anthropic)

    def complete(self, *, role, model, system, messages, max_tokens, effort=None, meta=None):
        self.ledger.check()
        worst = estimate_cost(model, estimate_tokens(request_text(system, messages)), max_tokens)
        self.ledger.reserve(worst)  # raises BudgetExceeded before anything is sent
        try:
            return self._complete(role, model, system, messages, max_tokens, effort, meta)
        finally:
            self.ledger.release(worst)

    def _complete(self, role, model, system, messages, max_tokens, effort, meta):
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
        worst = batch_max_cost(model, requests, max_tokens)
        if worst > self.ledger.remaining():
            raise BudgetExceeded(f"batch of {len(requests)} may cost ${worst:.2f}; "
                                 f"${self.ledger.remaining():.2f} left")
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

    Caps, all checked before a call is sent:
      per call        system + messages at most `max_request_bytes` UTF-8 bytes, which is
                      min(caps.max_prompt_chars, CHARS_PER_TOKEN x worker_tokens_per_task):
                      48 KB by default, so even at one token per byte every prompt stays
                      far below Haiku's 100K-token price cliff;
      per task        caps.worker_calls_per_task calls and caps.worker_tokens_per_task
                      tokens (the estimated input is charged before the call, then
                      corrected to the billed input + output);
      per evaluation  `total_cap` calls and `total_tokens` tokens, set by reset().
    "Per task" is keyed by the task id the agent sends, which the agent controls: it can
    spread calls over invented ids. The per-evaluation totals (calls and tokens per task
    times the number of tasks) are the real guarantee. A total cap of 0 refuses every
    call, and is the state outside an agent run; None means unlimited (tests only).
    Every call is logged in full (system, all messages, response) in the task's transcript.
    """

    def __init__(self, llm: LLM, model: str, effort: str | None, caps, sock_path: str | Path):
        self.llm, self.model, self.effort, self.caps = llm, model, effort, caps
        self.sock_path = str(sock_path)
        self.tasks: dict[str, TaskBudget] = {}
        self.total_calls = 0
        self.total_tokens_used = 0
        self.total_cap: int | None = 0  # set per evaluation by reset(); refuse until then
        self.total_tokens: int | None = 0
        self.max_request_bytes = min(caps.max_prompt_chars,
                                     CHARS_PER_TOKEN * caps.worker_tokens_per_task)
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingUnixStreamServer | None = None

    def handle(self, req: dict) -> dict:
        task = str(req.get("task", "?"))[:200]
        system = req.get("system", "")
        messages = req.get("messages") or []
        if not isinstance(system, str) or not isinstance(messages, list):
            return {"error": "bad request: system must be a string and messages a list"}
        text = request_text(system, messages)
        size = len(text.encode("utf-8", "replace"))
        if size > self.max_request_bytes:
            return {"error": f"prompt too long: {size} bytes > {self.max_request_bytes}"}
        est_in = estimate_tokens(text)
        max_tokens = max(1, min(int(req.get("max_tokens", 2000)), self.caps.worker_max_tokens))
        with self._lock:
            if self.total_cap is not None and self.total_calls >= self.total_cap:
                return {"error": "evaluation call cap reached"}
            if self.total_tokens is not None and self.total_tokens_used + est_in > self.total_tokens:
                return {"error": "evaluation token cap reached"}
            tb = self.tasks.setdefault(task, TaskBudget())
            if tb.calls >= self.caps.worker_calls_per_task:
                return {"error": "call cap reached"}
            if tb.tokens + est_in > self.caps.worker_tokens_per_task:
                return {"error": "token cap reached"}
            tb.calls += 1
            tb.tokens += est_in
            self.total_calls += 1
            self.total_tokens_used += est_in
        try:
            c = self.llm.complete(role="worker", model=self.model, system=system,
                                  messages=messages, max_tokens=max_tokens,
                                  effort=self.effort, meta={"task": task})
        except BudgetExceeded as e:
            with self._lock:
                tb.transcript.append({"system": system, "messages": messages,
                                      "max_tokens": max_tokens, "error": f"budget: {e}"})
            return {"error": f"budget: {e}"}
        billed = sum(c.usage.get(k, 0) for k in ("input_tokens", "output_tokens",
                                                 "cache_read_input_tokens",
                                                 "cache_creation_input_tokens"))
        with self._lock:
            tb.tokens += billed - est_in
            self.total_tokens_used += billed - est_in
            tb.transcript.append({"system": system, "messages": messages, "max_tokens": max_tokens,
                                  "response": c.text, "stop_reason": c.stop_reason,
                                  "usage": c.usage})
        if c.stop_reason == "refusal":
            return {"error": "refusal"}
        return {"text": c.text}

    def reset(self, total_cap: int | None, total_tokens: int | None = None) -> dict[str, TaskBudget]:
        """Start a new evaluation; returns the previous evaluation's per-task records.

        total_cap: calls allowed (0 refuses everything, None is unlimited).
        total_tokens: tokens allowed; by default worker_tokens_per_task for every
        worker_calls_per_task calls of total_cap."""
        if total_tokens is None and total_cap is not None:
            per_task = max(1, self.caps.worker_calls_per_task)
            total_tokens = -(-total_cap // per_task) * self.caps.worker_tokens_per_task
        with self._lock:
            prev, self.tasks = self.tasks, {}
            self.total_calls, self.total_cap = 0, total_cap
            self.total_tokens_used, self.total_tokens = 0, total_tokens
        return prev

    def start(self) -> None:
        proxy = self
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                # JSON escaping can at most ~6x a request; anything longer is refused unread.
                limit = 6 * proxy.max_request_bytes + 65_536
                while True:
                    line = self.rfile.readline(limit)
                    if not line:
                        return
                    if not line.endswith(b"\n"):
                        while line and not line.endswith(b"\n"):  # discard the rest unparsed
                            line = self.rfile.readline(limit)
                        self.wfile.write(b'{"error": "prompt too long"}\n')
                        self.wfile.flush()
                        continue
                    try:
                        out = proxy.handle(json.loads(line))
                    except Exception as e:  # malformed request from the agent
                        out = {"error": f"bad request: {type(e).__name__}"}
                    self.wfile.write((json.dumps(out) + "\n").encode())
                    self.wfile.flush()

        bind_path, dir_fd = self.sock_path, None
        if len(os.fsencode(self.sock_path)) > 100:  # AF_UNIX paths are limited to ~107 bytes
            dir_fd = os.open(os.path.dirname(os.path.abspath(self.sock_path)), os.O_RDONLY)
            bind_path = f"/proc/self/fd/{dir_fd}/{os.path.basename(self.sock_path)}"
        try:
            self._server = socketserver.ThreadingUnixStreamServer(bind_path, Handler)
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
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
