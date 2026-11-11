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
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://somewhere-else")
    with pytest.raises(RuntimeError):
        make_client(_Mod())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    m = _Mod()
    make_client(m)
    assert m.kwargs["api_key"] == "sk-test" and m.kwargs["base_url"] == API_BASE_URL
