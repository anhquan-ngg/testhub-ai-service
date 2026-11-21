"""Offline evidence that benchmark time belongs to one virtual event scheduler."""
import asyncio
import itertools
import time

import pytest
from google import genai

from scripts import benchmark_embedding_mock as benchmark


def run_virtual(coro):
    with asyncio.Runner(loop_factory=benchmark.VirtualEventLoop) as runner:
        return runner.run(coro)


def test_concurrent_sleeps_and_cancelled_deadlines_share_virtual_time():
    async def go():
        loop = asyncio.get_running_loop()
        completed = []

        async def work(delay):
            await asyncio.sleep(delay)
            completed.append(loop.time())

        await asyncio.gather(work(2), work(1), work(1))
        assert completed == [1.0, 1.0, 2.0]
        await asyncio.wait_for(work(0.25), timeout=40)
        await asyncio.sleep(0.25)
        assert completed[-1] == 2.25
        assert loop.time() == 2.5

    run_virtual(go())


@pytest.mark.parametrize("mode,success,requests,rejected,max_latency", [
    ("baseline", "1/10", 16, 9, 0.5),
    ("optimized", "5/10", 5, 0, 0.8),
    ("tuned", "8/10", 8, 0, 1.0),
])
def test_scenario_is_repeatable_despite_unrelated_host_clocks(
    monkeypatch, mode, success, requests, rejected, max_latency,
):
    original_client = genai.Client
    expected = run_virtual(benchmark.scenario(mode, 10, 20))
    ticks = itertools.count(1_000_000, 13)
    monkeypatch.setattr(time, "monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr(time, "time", lambda: float(next(ticks)))
    actual = run_virtual(benchmark.scenario(mode, 10, 20))
    assert actual == expected
    assert actual["success"] == success
    assert actual["http_requests"] == requests
    assert actual["http_429"] == rejected
    assert actual["max_s"] == max_latency
    assert genai.Client is original_client


@pytest.mark.parametrize("mode", ["baseline", "optimized"])
def test_job_deadline_cancels_server_latency_on_the_same_clock(monkeypatch, mode):
    monkeypatch.setattr(benchmark, "JOB_DEADLINE_S", 0.125)

    async def go():
        row = await benchmark.scenario(mode, 3, 5)
        assert row["success"] == "0/3"
        assert row["errors"] == "DEADLINE=3"
        assert row["http_requests"] == 0
        assert asyncio.get_running_loop().time() == 0.125
        # Cancelled HTTP/SDK deadlines must not survive scenario cleanup.
        await asyncio.sleep(0.125)
        assert asyncio.get_running_loop().time() == 0.25

    run_virtual(go())


def test_virtual_server_latency_controls_concurrency_waves(monkeypatch):
    monkeypatch.setattr(benchmark, "SERVER_LATENCY_S", 2.0)
    row = run_virtual(benchmark.scenario("optimized", 5, 10))
    assert row["success"] == "5/5"
    assert (row["p50_s"], row["max_s"]) == (4.0, 6.0)
    assert row["http_requests"] == 5 and row["http_429"] == 0


@pytest.mark.parametrize("server_hint,expected_elapsed", [(None, 1.25), ("1.5s", 2.0)])
def test_retry_backoff_uses_the_server_and_job_virtual_clock(monkeypatch, server_hint, expected_elapsed):
    original = benchmark.MockGemini.handle

    async def reject_once(self, request):
        if self.requests == 0:
            await asyncio.sleep(benchmark.SERVER_LATENCY_S)
            self.requests += 1
            self.rejected += 1
            details = [] if server_hint is None else [{
                "@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": server_hint,
            }]
            return benchmark.httpx.Response(429, json={"error": {
                "code": 429, "status": "RESOURCE_EXHAUSTED", "details": details,
            }})
        return await original(self, request)

    monkeypatch.setattr(benchmark.MockGemini, "handle", reject_once)

    async def go():
        row = await benchmark.scenario("optimized", 1, 1)
        assert row["success"] == "1/1"
        assert (row["http_requests"], row["http_429"]) == (2, 1)
        assert row["max_s"] == round(expected_elapsed, 1)
        assert asyncio.get_running_loop().time() == expected_elapsed

    run_virtual(go())


def test_sequential_same_plan_uses_cache_without_advancing_time():
    row = run_virtual(benchmark.scenario("optimized", 10, 20, repeat_same_plan=True))
    assert row["success"] == "10/10"
    assert row["http_requests"] == 1
    assert (row["p50_s"], row["max_s"]) == (0.0, 0.2)
