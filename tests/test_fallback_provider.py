"""Fallback only on fast availability failures; never on timeouts or invalid output."""
import asyncio
import pytest

from app.providers.base import ProviderError, ProviderTimeoutError, ProviderUnavailableError
from app.providers.fallback import FallbackProvider

class Stub:
    def __init__(self, error=None, result="ok"):
        self.error, self.result, self.calls, self.closed = error, result, 0, False
    async def generate(self, prompt):
        self.calls += 1
        if self.error: raise self.error
        return self.result
    generate_grounded = generate
    async def aclose(self): self.closed = True

def run(coro): return asyncio.run(coro)

def test_uses_fallback_when_primary_unavailable():
    primary, fallback = Stub(ProviderUnavailableError("x")), Stub(result="fb")
    provider = FallbackProvider(primary, fallback)
    assert run(provider.generate("p")) == "fb"
    assert run(provider.generate_grounded("p")) == "fb"
    assert primary.calls == 2 and fallback.calls == 2

@pytest.mark.parametrize("error", [ProviderTimeoutError("t"), ProviderError("bad")])
def test_no_fallback_for_timeout_or_other_errors(error):
    primary, fallback = Stub(error), Stub()
    with pytest.raises(type(error)):
        run(FallbackProvider(primary, fallback).generate("p"))
    assert fallback.calls == 0

def test_primary_success_skips_fallback_and_close_closes_both():
    primary, fallback = Stub(result="p"), Stub()
    provider = FallbackProvider(primary, fallback)
    assert run(provider.generate("x")) == "p" and fallback.calls == 0
    run(provider.aclose())
    assert primary.closed and fallback.closed

def test_fallback_failure_propagates():
    fallback_error = ProviderError("y")
    provider = FallbackProvider(Stub(ProviderUnavailableError("x")), Stub(fallback_error))
    with pytest.raises(ProviderError) as exc:
        run(provider.generate("p"))
    assert exc.value is fallback_error
