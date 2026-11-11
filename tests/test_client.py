import types

import pytest

from autopoiesis.llm import API_BASE_URL, make_client


class _Mod:
    def __init__(self):
        self.kwargs = None

    def Anthropic(self, **kw):  # noqa: N802 - mirrors the SDK
        self.kwargs = kw
        return types.SimpleNamespace(**kw)


def test_client_requires_owner_key_and_pins_endpoint(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PROJECTS_ANTHROPIC_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://somewhere-else")
    with pytest.raises(RuntimeError):
        make_client(_Mod())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    m = _Mod()
    make_client(m)
    assert m.kwargs["api_key"] == "sk-test" and m.kwargs["base_url"] == API_BASE_URL


def test_projects_key_takes_precedence(monkeypatch):
    monkeypatch.setenv("PROJECTS_ANTHROPIC_KEY", "sk-project")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-other")
    m = _Mod()
    make_client(m)
    assert m.kwargs["api_key"] == "sk-project"


def test_worker_thinking_can_be_disabled_per_role():
    from autopoiesis.llm import AnthropicLLM, Ledger

    sent = {}

    class _Msgs:
        def create(self, **kw):
            sent.update(kw)
            u = types.SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                                      cache_creation_input_tokens=0)
            return types.SimpleNamespace(usage=u, content=[], stop_reason="end_turn")

    import tempfile

    led = Ledger(tempfile.mktemp(suffix=".jsonl"), 1.0)
    llm = AnthropicLLM(led, client=types.SimpleNamespace(messages=_Msgs()), thinking={"worker": "disabled"})
    llm.complete(role="worker", model="claude-haiku-5-5", system="s",
                 messages=[{"role": "user", "content": "x"}], max_tokens=10, effort="low")
    assert sent["thinking"] == {"type": "disabled"}
    sent.clear()
    llm.complete(role="mutator", model="claude-sonnet-5-5", system="s",
                 messages=[{"role": "user", "content": "x"}], max_tokens=10)
    assert "thinking" not in sent
