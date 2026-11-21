"""Mock-only benchmark: baseline vs resilient embedding provider against a simulated RPM-limited server.

No network, no API key, no paid calls. The "server" is an httpx.MockTransport that counts embedded
items in a sliding 60 s window and answers 429 RESOURCE_EXHAUSTED + RetryInfo when the limit is
exceeded. A shared virtual event loop advances only when all runnable work is blocked, to the
next scheduled event. Concurrent jobs, server latency, backoff and deadlines use the same clock;
no host sleeps or wall-clock measurements affect results. Retry jitter is fixed at its midpoint.
Numbers model mock quota, not Gemini. Run with CPython 3.12 (the service's target runtime).

Usage: .venv\\Scripts\\python scripts\\benchmark_embedding_mock.py
"""
import asyncio
import heapq
import json
import logging
import math
import statistics
import sys
from collections import deque
from pathlib import Path
from unittest.mock import patch

import httpx
from google import genai

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.schemas.rag import BATCH_SIZE  # noqa: E402
from app.services import embedding as embedding_module  # noqa: E402
from app.services.embedding import GoogleEmbeddingProvider  # noqa: E402
from app.services.rag_errors import RagError  # noqa: E402

SERVER_ITEMS_PER_MIN = 100  # assumed mock limit; real quota is per project and not guaranteed
SERVER_LATENCY_S = 0.25
DUPLICATE_RATIO = 0.4      # assumed share of plan items repeating an objective+format already in the plan
JOB_DEADLINE_S = 40        # RagService operation deadline


class VirtualEventLoop(asyncio.SelectorEventLoop):
    """Discrete-event scheduler for mock-only tasks, including asyncio deadlines.

    Uses CPython's timer/ready queues so SDK wait_for timers share simulated time.
    No sockets, worker threads or other external I/O may drive this loop.
    """

    def __init__(self) -> None:
        self._now = 0.0
        super().__init__()

    def time(self) -> float:
        return self._now

    def _run_once(self) -> None:
        # Ignore cancelled deadlines rather than advancing time to abandoned work.
        while self._scheduled and self._scheduled[0].cancelled():
            timer = heapq.heappop(self._scheduled)
            timer._scheduled = False
            self._timer_cancelled_count -= 1
        if not self._ready:
            if not self._scheduled:
                raise RuntimeError("Virtual event loop has blocked work but no scheduled events")
            self._now = max(self._now, self._scheduled[0].when())
        super()._run_once()


class MockGemini:
    def __init__(self, items_per_min: int, scheduler: VirtualEventLoop) -> None:
        self.scheduler = scheduler
        self.limit, self.window, self.requests, self.rejected = items_per_min, deque(), 0, 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(SERVER_LATENCY_S)
        count = len(json.loads(request.content)["requests"])
        now = self.scheduler.time()
        while self.window and self.window[0][0] <= now - 60:
            self.window.popleft()
        self.requests += 1
        used = sum(n for _, n in self.window)
        if used + count > self.limit:
            self.rejected += 1
            retry = max(1, math.ceil(self.window[0][0] + 60 - now))
            return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
                "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{retry}s"}]}})
        self.window.append((now, count))
        return httpx.Response(200, json={"embeddings": [{"values": [1.0] * 768} for _ in range(count)]})

def plan_queries(job: int, questions: int) -> list[str]:
    distinct = max(1, math.ceil(questions * (1 - DUPLICATE_RATIO)))
    return [f"Job {job} topic: objective {i % distinct} (MULTIPLE_CHOICE)" for i in range(questions)]

async def baseline_job(provider: GoogleEmbeddingProvider, queries: list[str]) -> None:
    # Pre-change behaviour (HEAD 99ad516): no dedup, no cache, no limiter, one SDK attempt per batch of 16.
    for offset in range(0, len(queries), BATCH_SIZE):
        try:
            await provider._send(queries[offset:offset + BATCH_SIZE], "RETRIEVAL_QUERY")
        except genai.errors.APIError:
            raise RagError("RAG_EMBEDDING_RATE_LIMITED", 503, True) from None

async def optimized_job(provider: GoogleEmbeddingProvider, queries: list[str]) -> None:
    unique = list(dict.fromkeys(queries))
    for offset in range(0, len(unique), BATCH_SIZE):
        await provider.embed(unique[offset:offset + BATCH_SIZE], "RETRIEVAL_QUERY")

async def scenario(mode: str, jobs: int, questions: int, repeat_same_plan: bool = False) -> dict:
    scheduler = asyncio.get_running_loop()
    if not isinstance(scheduler, VirtualEventLoop):
        raise RuntimeError("Run the mock benchmark with VirtualEventLoop")
    server = MockGemini(SERVER_ITEMS_PER_MIN, scheduler)
    original = genai.Client

    def factory(**kwargs):
        kwargs["http_options"].async_client_args = {"transport": httpx.MockTransport(server.handle)}
        return original(**kwargs)

    with patch.object(embedding_module.genai, "Client", factory):
        if mode == "baseline":
            provider = GoogleEmbeddingProvider("mock", max_attempts=1, cache_size=0,
                                               sleep=asyncio.sleep, clock=scheduler.time)
            run = baseline_job
        else:  # optimized = shipped defaults; tuned = RPM set to the server limit
            rpm = SERVER_ITEMS_PER_MIN if mode == "tuned" else 60
            provider = GoogleEmbeddingProvider(
                "mock", max_rpm=rpm, max_concurrency=2, max_attempts=4, max_backoff_ms=8000,
                retry_budget_ms=15000, cache_size=2048, sleep=asyncio.sleep, clock=scheduler.time,
                wall_clock=scheduler.time, rng=lambda: 0.5,
            )
            run = optimized_job
    latencies, errors = [], {}

    async def one(job: int) -> None:
        started = scheduler.time()
        try:
            await asyncio.wait_for(run(provider, plan_queries(0 if repeat_same_plan else job, questions)),
                                   JOB_DEADLINE_S)
            latencies.append(scheduler.time() - started)
        except RagError as exc:
            errors[exc.code] = errors.get(exc.code, 0) + 1
        except TimeoutError:
            errors["DEADLINE"] = errors.get("DEADLINE", 0) + 1

    try:
        if repeat_same_plan:
            for job in range(jobs):
                await one(job)
        else:
            await asyncio.gather(*(one(job) for job in range(jobs)))
    finally:
        await provider.aclose()
    ok = len(latencies)
    return {
        "mode": mode, "jobs": jobs, "questions": questions, "success": f"{ok}/{jobs}",
        "http_requests": server.requests, "http_429": server.rejected,
        "p50_s": round(statistics.median(latencies), 1) if latencies else "-",
        "max_s": round(max(latencies), 1) if latencies else "-",
        "errors": ",".join(f"{k}={v}" for k, v in sorted(errors.items())) or "-",
    }

async def main() -> None:
    logging.disable(logging.CRITICAL)
    rows = []
    for questions in (1, 5, 10, 20):
        for mode in ("baseline", "optimized"):
            rows.append(await scenario(mode, 1, questions))
    for jobs, questions in ((5, 10), (10, 20), (50, 5), (100, 5)):
        for mode in ("baseline", "optimized", "tuned"):
            rows.append(await scenario(mode, jobs, questions))
    for mode in ("baseline", "optimized"):
        row = await scenario(mode, 10, 20, repeat_same_plan=True)
        row["mode"] += " (same plan x10, sequential)"
        rows.append(row)
    print(f"mock server limit={SERVER_ITEMS_PER_MIN} items/min, latency={SERVER_LATENCY_S}s, "
          f"duplicate_ratio={DUPLICATE_RATIO}, job_deadline={JOB_DEADLINE_S}s")
    headers = list(rows[0])
    print("| " + " | ".join(headers) + " |")
    print("|" + "---|" * len(headers))
    for row in rows:
        print("| " + " | ".join(str(row[h]) for h in headers) + " |")

if __name__ == "__main__":
    with asyncio.Runner(loop_factory=VirtualEventLoop) as runner:
        runner.run(main())
