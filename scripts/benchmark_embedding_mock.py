"""Mock-only benchmark: baseline vs resilient embedding provider against a simulated RPM-limited server.

No network, no API key, no paid calls. The "server" is an httpx.MockTransport that counts embedded
items in a sliding 60 s window and answers 429 RESOURCE_EXHAUSTED + RetryInfo when the limit is
exceeded. Time is scaled (SCALE real seconds per simulated second) so latencies are simulated and
approximate (+/- ~0.3 s from the OS timer). Numbers describe this model only, not real Gemini quota.

Usage: .venv\\Scripts\\python scripts\\benchmark_embedding_mock.py
"""
import asyncio
import json
import logging
import math
import statistics
import sys
import time
from collections import deque
from pathlib import Path

import httpx
from google import genai

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.schemas.rag import BATCH_SIZE  # noqa: E402
from app.services import embedding as embedding_module  # noqa: E402
from app.services.embedding import GoogleEmbeddingProvider  # noqa: E402
from app.services.rag_errors import RagError  # noqa: E402

SCALE = 0.05               # real seconds per simulated second
SERVER_ITEMS_PER_MIN = 100  # assumed mock limit; real quota is per project and not guaranteed
SERVER_LATENCY_S = 0.25
DUPLICATE_RATIO = 0.4      # assumed share of plan items repeating an objective+format already in the plan
JOB_DEADLINE_S = 40        # RagService operation deadline

def sim_clock() -> float:
    return time.monotonic() / SCALE

async def sim_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds * SCALE)

class MockGemini:
    def __init__(self, items_per_min: int) -> None:
        self.limit, self.window, self.requests, self.rejected = items_per_min, deque(), 0, 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await sim_sleep(SERVER_LATENCY_S)
        count = len(json.loads(request.content)["requests"])
        now = sim_clock()
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

CURRENT: dict[str, MockGemini] = {}

def install(server: MockGemini) -> None:
    """Route the SDK's async client to a fresh mock server per scenario (patched once, no stacking)."""
    CURRENT["server"] = server
    if "original" not in CURRENT:
        original = CURRENT["original"] = genai.Client
        def factory(**kwargs):
            target = CURRENT["server"]
            kwargs["http_options"].async_client_args = {"transport": httpx.MockTransport(target.handle)}
            return original(**kwargs)
        embedding_module.genai.Client = factory

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
    server = MockGemini(SERVER_ITEMS_PER_MIN)
    install(server)
    if mode == "baseline":
        provider = GoogleEmbeddingProvider("mock", max_attempts=1, cache_size=0, sleep=sim_sleep, clock=sim_clock)
        run = baseline_job
    else:  # "optimized" = shipped defaults from app/config.py; "tuned" = EMBEDDING_MAX_RPM set to the server limit
        rpm = SERVER_ITEMS_PER_MIN if mode == "tuned" else 60
        provider = GoogleEmbeddingProvider("mock", max_rpm=rpm, max_concurrency=2, max_attempts=4, max_backoff_ms=8000,
                                           retry_budget_ms=15000, cache_size=2048, sleep=sim_sleep, clock=sim_clock)
        run = optimized_job
    latencies, errors = [], {}

    async def one(job: int) -> None:
        started = sim_clock()
        try:
            await asyncio.wait_for(run(provider, plan_queries(0 if repeat_same_plan else job, questions)),
                                   JOB_DEADLINE_S * SCALE)
            latencies.append(sim_clock() - started)
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
    asyncio.run(main())
