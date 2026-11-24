"""429 resilience, cache and limiter for the Google embedding adapter (MockTransport, fake clock; no paid calls)."""
import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from google import genai

from app.config import Settings
from app.schemas.grounded_generation import GroundedGenerateRequest
from app.schemas.rag import current_profile
from app.services.embedding import EmbeddingUsage, GoogleEmbeddingProvider, RateLimiter
from app.services.rag_errors import RagError
from app.services.chunking import chunk_document
from app.services.retrieval import RagService
from tests.test_rag import source

RETRY_INFO = "type.googleapis.com/google.rpc.RetryInfo"
QUOTA_FAILURE = "type.googleapis.com/google.rpc.QuotaFailure"

class FakeTime:
    def __init__(self):
        self.now, self.sleeps = 1_000.0, []
    def clock(self):
        return self.now
    async def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds

def ok(req):
    count = len(json.loads(req.content)["requests"])
    return httpx.Response(200, json={"embeddings": [{"values": [1.0] * 768} for _ in range(count)]})

def rate_limited(details=None, headers=None):
    return httpx.Response(429, headers=headers or {}, json={"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "PRIVATE-SOURCE", "details": details or []}})

def install(monkeypatch, responses):
    """Serve queued responses (callables or Response) then succeed; record request bodies."""
    calls = []
    def handler(req):
        calls.append(json.loads(req.content))
        nxt = responses.pop(0) if responses else ok
        return nxt(req) if callable(nxt) else nxt
    original = genai.Client
    def factory(**kwargs):
        kwargs["http_options"].async_client_args = {"transport": httpx.MockTransport(handler)}
        return original(**kwargs)
    monkeypatch.setattr("app.services.embedding.genai.Client", factory)
    return calls

def provider(fake, **kwargs):
    kwargs = {"max_attempts": 4, "rng": lambda: 0.5, **kwargs}
    return GoogleEmbeddingProvider("fixture-key", sleep=fake.sleep, clock=fake.clock, **kwargs)

def run(coro_factory):
    async def wrapper():
        return await coro_factory()
    return asyncio.run(wrapper())

def test_429_then_success_uses_jittered_backoff(monkeypatch, caplog):
    caplog.set_level("INFO")
    fake = FakeTime(); calls = install(monkeypatch, [rate_limited(), rate_limited()])
    async def go():
        p = provider(fake)
        try:
            assert len(await p.embed(["LIFO"], "RETRIEVAL_QUERY")) == 1
            return p.stats
        finally:
            await p.aclose()
    stats = run(go)
    assert len(calls) == 3 and stats["retries"] == 2 and stats["rate_limited"] == 2
    # rng=0.5 -> ceiling/2 + 0.25*ceiling with ceiling 1s then 2s.
    assert fake.sleeps == [0.75, 1.5]
    assert "outcome=ok" in caplog.text and "attempts=3" in caplog.text
    assert "PRIVATE-SOURCE" not in caplog.text and "LIFO" not in caplog.text

@pytest.mark.parametrize("response,expected", [
    (lambda r: rate_limited(headers={"retry-after": "3"}), 3.0),
    (lambda r: rate_limited([{"@type": RETRY_INFO, "retryDelay": "2s"}]), 2.0),
])
def test_server_retry_hint_is_honored(monkeypatch, response, expected):
    fake = FakeTime(); calls = install(monkeypatch, [response])
    async def go():
        p = provider(fake)
        try:
            await p.embed(["LIFO"], "RETRIEVAL_QUERY")
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 2 and fake.sleeps == [expected]

def test_exhausted_or_oversized_hint_fails_fast_as_retryable_rate_limit(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [rate_limited([{"@type": RETRY_INFO, "retryDelay": "45s"}])])
    async def go():
        p = provider(fake, max_backoff_ms=8_000)
        try:
            with pytest.raises(RagError) as error:
                await p.embed(["LIFO"], "RETRIEVAL_QUERY")
            return error.value
        finally:
            await p.aclose()
    error = run(go)
    assert (error.code, error.status, error.retryable) == ("RAG_EMBEDDING_RATE_LIMITED", 503, True)
    assert len(calls) == 1 and fake.sleeps == []

@pytest.mark.parametrize("failure,expected_sends", [
    ("breaker", 0), ("limiter", 0), ("config", 1), ("invalid", 1),
    ("timeout", 1), ("transport", 3), ("rate_limit", 3),
])
def test_failed_call_logs_actual_sends(monkeypatch, caplog, failure, expected_sends):
    caplog.set_level("INFO", logger="app.services.embedding")
    fake = FakeTime()
    responses = {
        "config": [httpx.Response(403, json={"error": {"code": 403}})],
        "invalid": [httpx.Response(200, json={"embeddings": []})],
        "rate_limit": [rate_limited() for _ in range(3)],
    }
    calls = install(monkeypatch, responses.get(failure, []))

    async def go():
        p = provider(fake, max_attempts=3, max_rpm=1 if failure == "limiter" else None)
        if failure == "breaker":
            p._quota_open_until = fake.now + 300
        if failure == "limiter":
            p.limiter.charge(1)
        if failure in {"timeout", "transport"}:
            async def fail(texts, task):
                if failure == "timeout":
                    raise httpx.ReadTimeout("PRIVATE-SOURCE")
                raise httpx.ConnectError("PRIVATE-SOURCE")
            monkeypatch.setattr(p, "_send", fail)
        try:
            with pytest.raises(RagError):
                await p.embed(["PRIVATE-SOURCE"], "RETRIEVAL_QUERY")
            assert p.stats["api_requests"] == expected_sends
        finally:
            await p.aclose()

    run(go)
    summaries = [record.getMessage() for record in caplog.records
                 if record.getMessage().startswith("embedding_call ")]
    assert len(summaries) == 1
    assert f"attempts={expected_sends} " in summaries[0]
    assert "PRIVATE-SOURCE" not in caplog.text
    if failure not in {"timeout", "transport"}:
        assert len(calls) == expected_sends


def test_overlapping_failed_calls_log_only_their_own_sends(monkeypatch, caplog):
    caplog.set_level("INFO", logger="app.services.embedding")
    fake = FakeTime()
    install(monkeypatch, [rate_limited(), rate_limited()])

    async def go():
        p = provider(fake, max_attempts=1, cache_size=0)
        entered, release = asyncio.Event(), asyncio.Event()
        original = p._send

        async def controlled(texts, task):
            if texts == ["first"]:
                entered.set()
                await release.wait()
            return await original(texts, task)

        monkeypatch.setattr(p, "_send", controlled)
        first = asyncio.create_task(p.embed(["first"], "RETRIEVAL_QUERY"))
        try:
            await entered.wait()
            with pytest.raises(RagError):
                await p.embed(["second"], "RETRIEVAL_QUERY")
            release.set()
            with pytest.raises(RagError):
                await first
            assert p.stats["api_requests"] == 2
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await p.aclose()

    run(go)
    summaries = [record.getMessage() for record in caplog.records
                 if record.getMessage().startswith("embedding_call ")]
    assert len(summaries) == 2
    assert all("attempts=1 " in summary for summary in summaries)


def test_attempts_exhausted(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [rate_limited() for _ in range(9)])
    async def go():
        p = provider(fake, max_attempts=3)
        try:
            with pytest.raises(RagError, match="RAG_EMBEDDING_RATE_LIMITED"):
                await p.embed(["LIFO"], "RETRIEVAL_QUERY")
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 3

def test_daily_quota_is_not_retried_and_opens_breaker(monkeypatch):
    daily = rate_limited([{"@type": QUOTA_FAILURE, "violations": [
        {"quotaMetric": "generativelanguage.googleapis.com/embed_content_requests",
         "quotaId": "EmbedContentRequestsPerDayPerProjectPerModel-FreeTier"}]}])
    fake = FakeTime(); calls = install(monkeypatch, [daily])
    async def go():
        p = provider(fake)
        try:
            for _ in range(2):
                with pytest.raises(RagError) as error:
                    await p.embed(["LIFO"], "RETRIEVAL_QUERY")
                assert (error.value.code, error.value.retryable) == ("RAG_EMBEDDING_QUOTA_EXHAUSTED", False)
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 1 and fake.sleeps == []

def test_config_error_not_retried_and_transient_5xx_retried(monkeypatch):
    fake = FakeTime()
    calls = install(monkeypatch, [httpx.Response(403, json={"error": {"code": 403, "message": "x"}}),
                                  httpx.Response(503, json={"error": {"code": 503, "message": "x"}})])
    async def go():
        p = provider(fake)
        try:
            with pytest.raises(RagError, match="RAG_EMBEDDING_CONFIG"):
                await p.embed(["a"], "RETRIEVAL_QUERY")
            await p.embed(["b"], "RETRIEVAL_QUERY")
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 3 and fake.sleeps == [0.75]

def test_cache_and_in_call_dedup_are_task_scoped(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake)
        try:
            first = await p.embed(["A", "B", "A"], "RETRIEVAL_QUERY")
            first[0][0] = 99.0  # callers must not be able to corrupt the cache
            second = await p.embed(["B", "A", "C"], "RETRIEVAL_QUERY")
            await p.embed(["A"], "RETRIEVAL_DOCUMENT")
            return first, second, p.stats
        finally:
            await p.aclose()
    first, second, stats = run(go)
    assert [len(c["requests"]) for c in calls] == [2, 1, 1]
    assert [r["content"]["parts"][0]["text"] for r in calls[1]["requests"]] == ["C"]
    assert calls[2]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"
    assert len(first) == 3 and second[1][0] != 99.0
    assert stats["cache_hits"] == 2 and stats["deduplicated"] == 1 and stats["api_items"] == 4

def test_cache_is_bounded_and_can_be_disabled(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        bounded, disabled = provider(fake, cache_size=1), provider(fake, cache_size=0)
        try:
            for text in ("A", "B", "A"):
                await bounded.embed([text], "RETRIEVAL_QUERY")
            for _ in range(2):
                await disabled.embed(["A"], "RETRIEVAL_QUERY")
        finally:
            await bounded.aclose(); await disabled.aclose()
    run(go)
    assert len(calls) == 5

def test_limiter_spaces_items_over_rolling_minute():
    fake = FakeTime(); limiter = RateLimiter(10, clock=fake.clock, sleep=fake.sleep)
    async def go():
        for _ in range(3):
            await limiter.acquire(5, max_wait=120)
    run(go)
    assert fake.sleeps == [60.0]

def test_limiter_refuses_to_wait_past_budget_and_shares_429_pause():
    fake = FakeTime(); limiter = RateLimiter(5, clock=fake.clock, sleep=fake.sleep)
    async def go():
        await limiter.acquire(5, max_wait=10)
        with pytest.raises(RagError) as error:
            await limiter.acquire(1, max_wait=10)
        assert (error.value.code, error.value.retryable) == ("RAG_EMBEDDING_RATE_LIMITED", True)
        open_limiter = RateLimiter(None, clock=fake.clock, sleep=fake.sleep)
        open_limiter.pause(4)
        await open_limiter.acquire(1, max_wait=10)
    run(go)
    assert fake.sleeps == [4.0]

def test_concurrency_cap(monkeypatch):
    fake = FakeTime()
    calls = install(monkeypatch, [])
    active = peak = 0

    async def go():
        p = provider(fake, max_concurrency=2, cache_size=0)
        entered, release = asyncio.Event(), asyncio.Event()
        original = p._send

        async def controlled(texts, task):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
                return await original(texts, task)
            finally:
                active -= 1

        monkeypatch.setattr(p, "_send", controlled)
        jobs = [asyncio.create_task(p.embed([f"t{i}"], "RETRIEVAL_QUERY")) for i in range(6)]
        try:
            await asyncio.sleep(0)  # Let every job reach admission, without advancing time.
            assert entered.is_set() and active == 2
            assert p.stats["api_requests"] == 2
            fake.now += 0.25
            release.set()
            await asyncio.gather(*jobs)
        finally:
            release.set()
            await asyncio.gather(*jobs, return_exceptions=True)
            await p.aclose()

    run(go)
    assert peak == 2 and active == 0 and len(calls) == 6

def test_embedding_settings_bounds_and_env(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MAX_RPM", "30")
    monkeypatch.setenv("EMBEDDING_CACHE_SIZE", "0")
    settings = Settings.from_env()
    assert (settings.embedding_max_rpm, settings.embedding_cache_size, settings.embedding_max_attempts) == (30, 0, 4)
    with pytest.raises(ValueError, match="EMBEDDING_MAX_ATTEMPTS"):
        Settings(embedding_max_attempts=0)
    with pytest.raises(ValueError, match="EMBEDDING_RETRY_BUDGET_MS"):
        Settings(embedding_retry_budget_ms=40_000)

def test_plan_retrieval_embeds_duplicate_objectives_once():
    profile = current_profile().profile_id
    index_id = uuid4()
    repo = AsyncMock(); embedding = AsyncMock()
    embedding.embed.side_effect = lambda texts, task: [[float(i + 1)] + [0.0] * 767 for i in range(len(texts))]
    chunk = chunk_document(index_id, source("Stack uses LIFO."))[0]
    repo.search.return_value = [{**chunk.model_dump(), "index_id": index_id, "document_id": uuid4(),
                                 "version_id": uuid4(), "source_sha256": "a" * 64, "similarity": 0.9}]
    config = {"question_type": "SINGLE_CHOICE", "question_format": "KNOWLEDGE", "count": 1}
    items = [{**config, "id": f"cell-{i}", "knowledge_title": "Stack", "objective": "Explain LIFO" if i < 4 else "Push",
              "source_units": [{"kind": "PAGE", "position": 1}]} for i in range(5)]
    request = GroundedGenerateRequest(owner_id=uuid4(), profile_id=profile, index_ids=[index_id], query="Stack",
                                      question_configs=[config] * 5, plan_items=items)
    contexts = asyncio.run(RagService(repo, embedding).retrieve_plan(request))
    assert embedding.embed.await_count == 1
    assert len(embedding.embed.await_args.args[0]) == 2
    searched = [call.args[3][0] for call in repo.search.await_args_list]
    assert searched == [1.0, 1.0, 1.0, 1.0, 2.0] and len(contexts) == 5

@pytest.mark.parametrize("spent", [0, 1])
def test_over_rpm_batch_rejected_without_charging_and_boundary_accepted(monkeypatch, spent):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake, max_rpm=2)
        try:
            if spent:
                await p.embed(["already sent"], "RETRIEVAL_QUERY")
            usage = EmbeddingUsage()
            with pytest.raises(RagError, match="RAG_EMBEDDING_INPUT_INVALID"):
                await p.embed(["a", "b", "c"], "RETRIEVAL_QUERY", usage=usage)
            assert not usage.upstream_started
            assert p.limiter._used == spent
            fake.now += 60
            await p.embed(["a", "b"], "RETRIEVAL_QUERY")
            assert p.limiter._used == 2
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == spent + 1 and fake.sleeps == []

@pytest.mark.parametrize("header,expected", [
    ("Thu, 01 Jan 1970 00:16:43 GMT", 3.0),
    ("Thu, 01 Jan 1970 00:16:39 GMT", None),
    ("not-a-date", 2.0),
])
def test_http_date_retry_after_future_past_and_invalid(monkeypatch, header, expected):
    fake = FakeTime()
    calls = install(monkeypatch, [rate_limited([
        {"@type": RETRY_INFO, "retryDelay": "2s"}], headers={"retry-after": header})])
    async def go():
        p = provider(fake, wall_clock=fake.clock)
        try:
            await p.embed(["text"], "RETRIEVAL_QUERY")
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 2
    assert fake.sleeps == ([] if expected is None else [expected])

@pytest.mark.parametrize("daily", [False, True])
def test_waiting_semaphore_rechecks_new_cooldown_and_breaker(monkeypatch, daily):
    fake = FakeTime()
    details = [{"@type": QUOTA_FAILURE, "violations": [{"quotaId": "RequestsPerDay"}]}] if daily else []
    calls = install(monkeypatch, [rate_limited(details, headers={"retry-after": "3"})])
    original = GoogleEmbeddingProvider._send
    async def go():
        p = provider(fake, max_concurrency=1, max_attempts=1, cache_size=0)
        entered, finish = asyncio.Event(), asyncio.Event()
        sent_at = []
        async def controlled(texts, task):
            sent_at.append(fake.clock())
            if len(sent_at) == 1:
                entered.set()
                await finish.wait()
            return await original(p, texts, task)
        monkeypatch.setattr(p, "_send", controlled)
        try:
            first = asyncio.create_task(p.embed(["first"], "RETRIEVAL_QUERY"))
            await entered.wait()
            usage = EmbeddingUsage()
            second = asyncio.create_task(p.embed(["second"], "RETRIEVAL_QUERY", usage=usage))
            await asyncio.sleep(0)
            assert p.stats["api_requests"] == 1
            finish.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert isinstance(results[0], RagError)
            if daily:
                assert isinstance(results[1], RagError)
                assert results[1].code == "RAG_EMBEDDING_QUOTA_EXHAUSTED"
                assert not usage.upstream_started
                assert sent_at == [1000.0] and fake.sleeps == []
            else:
                assert isinstance(results[1], list)
                assert usage.upstream_started
                assert sent_at == [1000.0, 1003.0] and fake.sleeps == [3.0]
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == (1 if daily else 2)


def test_rpm_timestamp_is_send_time_not_semaphore_queue_time(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake, max_rpm=1, max_concurrency=1, retry_budget_ms=15_000)
        await p._slots.acquire()
        try:
            queued = asyncio.create_task(p.embed(["first"], "RETRIEVAL_QUERY"))
            await asyncio.sleep(0)
            assert p.limiter._used == 0 and p.stats["api_requests"] == 0
            fake.now += 61
            p._slots.release()
            await queued
            assert list(p.limiter._window) == [(1061.0, 1, False)]
            usage = EmbeddingUsage()
            with pytest.raises(RagError, match="RAG_EMBEDDING_RATE_LIMITED"):
                await p.embed(["second"], "RETRIEVAL_QUERY", usage=usage)
            assert not usage.upstream_started
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 1


@pytest.mark.parametrize("after_send", [False, True])
def test_cancellation_usage_distinguishes_unsent_from_ambiguous(monkeypatch, caplog, after_send):
    caplog.set_level("INFO", logger="app.services.embedding")
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake, max_concurrency=1)
        entered = asyncio.Event()
        async def hold(texts, task):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(p, "_send", hold)
        if not after_send:
            await p._slots.acquire()
        usage = EmbeddingUsage()
        task = asyncio.create_task(p.embed(["source"], "RETRIEVAL_DOCUMENT", usage=usage))
        try:
            if after_send:
                await entered.wait()
            else:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert usage.upstream_started is after_send
            assert p.stats["api_requests"] == int(after_send)
        finally:
            if not after_send:
                p._slots.release()
            await p.aclose()
    run(go)
    assert not calls
    summaries = [record.getMessage() for record in caplog.records
                 if record.getMessage().startswith("embedding_call ")]
    assert len(summaries) == 1
    assert f"attempts={int(after_send)} outcome=cancelled" in summaries[0]

@pytest.mark.parametrize("response", [rate_limited(), httpx.Response(503, json={"error": {"code": 503}})])
def test_exhausted_sent_call_never_becomes_refundable(monkeypatch, response):
    fake = FakeTime(); calls = install(monkeypatch, [response])
    async def go():
        p = provider(fake, max_attempts=1)
        usage = EmbeddingUsage()
        try:
            with pytest.raises(RagError):
                await p.embed(["source"], "RETRIEVAL_DOCUMENT", usage=usage)
            assert usage.upstream_started
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 1


def test_index_step_refunds_only_proven_unsent_work_and_cached_success(monkeypatch):
    from tests.test_rag import command
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake)
        repo = AsyncMock(); req = command()
        row = {"id": req.index_id, "profile_id": req.profile_id, "status": "BUILDING", "chunk_count": 1, "completed_chunks": 0}
        repo.claim.return_value = (row, [{"id": uuid4(), "text": "source"}])
        repo.save.return_value = {**row, "status": "READY", "completed_chunks": 1}
        service = RagService(repo, p)
        try:
            p._quota_open_until = fake.now + 300
            for _ in range(4):
                with pytest.raises(RagError, match="RAG_EMBEDDING_QUOTA_EXHAUSTED"):
                    await service.step(req)
                assert repo.release.await_args.kwargs == {"refund_attempt": True}
            assert not calls
            fake.now += 300
            await service.step(req)
            assert repo.save.await_args.kwargs == {}
            assert repo.release.await_args.kwargs == {}
            await service.step(req)
            assert repo.save.await_args.kwargs == {"refund_attempt": True}
            assert repo.release.await_args.kwargs == {"refund_attempt": True}
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 1

@pytest.mark.parametrize("failure", ["timeout", "transport", "cancel_before_send", "cancel_after_send"])
def test_index_step_preserves_ambiguous_debit_but_refunds_unsent_cancellation(monkeypatch, failure):
    from tests.test_rag import command
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake, max_concurrency=1, max_attempts=1)
        repo = AsyncMock(); req = command()
        row = {"id": req.index_id, "profile_id": req.profile_id, "status": "BUILDING", "chunk_count": 1, "completed_chunks": 0}
        repo.claim.return_value = (row, [{"id": uuid4(), "text": "source"}])
        entered = asyncio.Event()
        async def fail(texts, task):
            entered.set()
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic")
            if failure == "transport":
                raise httpx.ConnectError("synthetic")
            await asyncio.Event().wait()
        monkeypatch.setattr(p, "_send", fail)
        if failure == "cancel_before_send":
            await p._slots.acquire()
        try:
            task = asyncio.create_task(RagService(repo, p).step(req))
            if failure.startswith("cancel"):
                if failure == "cancel_before_send":
                    await asyncio.sleep(0)
                else:
                    await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(RagError):
                    await task
            repo.save.assert_not_awaited()
            expected = {"refund_attempt": True} if failure == "cancel_before_send" else {}
            assert repo.release.await_args.kwargs == expected
        finally:
            if failure == "cancel_before_send":
                p._slots.release()
            await p.aclose()
    run(go)
    assert not calls


def test_http_date_above_retry_budget_is_not_slept_or_retried(monkeypatch):
    fake = FakeTime()
    calls = install(monkeypatch, [rate_limited(headers={"retry-after": "Thu, 01 Jan 1970 00:17:00 GMT"})])
    async def go():
        p = provider(fake, wall_clock=fake.clock)
        usage = EmbeddingUsage()
        try:
            with pytest.raises(RagError, match="RAG_EMBEDDING_RATE_LIMITED"):
                await p.embed(["source"], "RETRIEVAL_DOCUMENT", usage=usage)
            assert usage.upstream_started
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 1 and not fake.sleeps


def test_repeated_breaker_claims_refund_only_the_reservation_not_prior_sends(monkeypatch):
    from tests.test_rag import command
    fake = FakeTime()
    daily = rate_limited([{"@type": QUOTA_FAILURE, "violations": [{"quotaId": "RequestsPerDay"}]}])
    calls = install(monkeypatch, [daily])
    async def go():
        p = provider(fake)
        req, repo = command(), AsyncMock()
        row = {"id": req.index_id, "profile_id": req.profile_id, "status": "BUILDING", "chunk_count": 1, "completed_chunks": 0}
        attempts = 1  # An earlier upstream invocation must remain spent.
        async def claim(request, token):
            nonlocal attempts
            if attempts >= 3:
                raise RagError("RAG_RETRY_EXHAUSTED", 409)
            attempts += 1
            return row, [{"id": uuid4(), "text": "source"}]
        async def release(index_id, token, *, refund_attempt=False):
            nonlocal attempts
            attempts -= int(refund_attempt)
        repo.claim.side_effect, repo.release.side_effect = claim, release
        repo.save.return_value = {**row, "status": "READY", "completed_chunks": 1}
        service = RagService(repo, p)
        try:
            for _ in range(6):
                with pytest.raises(RagError, match="RAG_EMBEDDING_QUOTA_EXHAUSTED"):
                    await service.step(req)
                assert attempts == 2
            assert repo.release.await_args_list[0].kwargs == {}
            assert all(call.kwargs == {"refund_attempt": True} for call in repo.release.await_args_list[1:])
            fake.now += 301
            assert (await service.step(req)).status == "READY"
            assert attempts == 3
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 2


class FakeStore:
    def __init__(self, data=None, fail=False):
        self.data, self.fail, self.gets, self.puts = dict(data or {}), fail, 0, 0
    async def get_many(self, keys):
        self.gets += 1
        if self.fail:
            raise RuntimeError("relation rag.embedding_cache does not exist")
        return {key: self.data[key] for key in keys if key in self.data}
    async def put_many(self, items):
        self.puts += 1
        if self.fail:
            raise RuntimeError("down")
        self.data.update(items)

def docs(count, prefix="doc"):
    return [f"{prefix}-{i}" for i in range(count)]

def test_indexing_is_capped_to_share_while_queries_still_fit(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake, max_rpm=60, indexing_rpm_share=0.5)
        try:
            assert p.limiter.background_cap == 30
            await p.embed(docs(16, "a"), "RETRIEVAL_DOCUMENT")
            with pytest.raises(RagError) as error:
                await p.embed(docs(16, "b"), "RETRIEVAL_DOCUMENT")
            assert (error.value.code, error.value.retryable) == ("RAG_EMBEDDING_RATE_LIMITED", True)
            assert len(await p.embed(["interactive"], "RETRIEVAL_QUERY")) == 1
            return p.limiter._used
        finally:
            await p.aclose()
    assert run(go) == 17 and len(calls) == 2

def test_indexing_cap_is_released_when_window_expires(monkeypatch):
    fake = FakeTime(); install(monkeypatch, [])
    async def go():
        p = provider(fake, max_rpm=60, indexing_rpm_share=0.5, retry_budget_ms=120_000)
        try:
            await p.embed(docs(16, "a"), "RETRIEVAL_DOCUMENT")
            await p.embed(docs(16, "b"), "RETRIEVAL_DOCUMENT")
            return p.limiter._background_used
        finally:
            await p.aclose()
    assert run(go) == 16 and sum(fake.sleeps) >= 59

def test_full_share_disables_indexing_cap():
    limiter = RateLimiter(60, clock=lambda: 0.0, sleep=None, background_share=1.0)
    assert limiter.background_cap is None
    assert RateLimiter(None, clock=lambda: 0.0, sleep=None, background_share=0.5).background_cap is None

def test_indexing_yields_while_a_query_is_in_flight(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake, max_rpm=600, indexing_rpm_share=0.5)
        original = fake.sleep
        async def sleep(seconds):
            p._interactive = 0
            await original(seconds)
        p._sleep = sleep
        p._interactive = 1
        try:
            await p.embed(docs(2), "RETRIEVAL_DOCUMENT")
            return p.stats["indexing_yields"]
        finally:
            await p.aclose()
    assert run(go) == 1 and len(calls) == 1 and fake.sleeps == [0.25]

def test_queries_never_yield_to_indexing(monkeypatch):
    fake = FakeTime(); install(monkeypatch, [])
    async def go():
        p = provider(fake, max_rpm=600, indexing_rpm_share=0.5)
        p._interactive = 5
        try:
            await p.embed(["q"], "RETRIEVAL_QUERY")
            return p.stats["indexing_yields"]
        finally:
            await p.aclose()
    assert run(go) == 0 and fake.sleeps == []

def test_indexing_concurrency_slot_does_not_block_queries(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake, indexing_max_concurrency=1)
        try:
            await p._indexing_slots.acquire()
            indexing = asyncio.create_task(p.embed(docs(2), "RETRIEVAL_DOCUMENT"))
            await asyncio.sleep(0)
            assert await p.embed(["q"], "RETRIEVAL_QUERY") and not indexing.done() and len(calls) == 1
            p._indexing_slots.release()
            await indexing
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 2
    assert provider(FakeTime())._indexing_slots is None

def test_persistent_cache_serves_queries_and_fills_l1(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [])
    key = GoogleEmbeddingProvider._key("RETRIEVAL_QUERY", "LIFO")
    store = FakeStore({key: [0.5] * 768})
    async def go():
        p = provider(fake); p.attach_persistent_cache(store)
        try:
            first = await p.embed(["LIFO"], "RETRIEVAL_QUERY")
            again = await p.embed(["LIFO"], "RETRIEVAL_QUERY")
            return first, again, p.stats
        finally:
            await p.aclose()
    first, again, stats = run(go)
    assert calls == [] and first == again and stats["persistent_hits"] == 1 and store.gets == 1

def test_persistent_cache_miss_writes_new_query_vectors(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake); store = FakeStore(); p.attach_persistent_cache(store)
        try:
            await p.embed(["LIFO", "FIFO"], "RETRIEVAL_QUERY")
            return store
        finally:
            await p.aclose()
    store = run(go)
    assert len(calls) == 1 and len(store.data) == 2 and store.puts == 1
    assert all(len(key) == 64 and "LIFO" not in key for key in store.data)

def test_persistent_cache_is_query_only(monkeypatch):
    fake = FakeTime(); install(monkeypatch, [])
    async def go():
        p = provider(fake); store = FakeStore(); p.attach_persistent_cache(store)
        try:
            await p.embed(["chunk text"], "RETRIEVAL_DOCUMENT")
            return store
        finally:
            await p.aclose()
    store = run(go)
    assert (store.gets, store.puts, store.data) == (0, 0, {})

def test_persistent_cache_failure_fails_open(monkeypatch, caplog):
    caplog.set_level("WARNING")
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake); p.attach_persistent_cache(FakeStore(fail=True))
        try:
            assert len(await p.embed(["LIFO"], "RETRIEVAL_QUERY")) == 1
            return p.stats
        finally:
            await p.aclose()
    stats = run(go)
    assert len(calls) == 1 and stats["persistent_errors"] == 2
    assert "embedding_persistent_cache_unavailable" in caplog.text and "LIFO" not in caplog.text

def test_embedding_key_falls_back_and_indexing_settings_validate(monkeypatch):
    base = {"llm_provider": "gemini", "gemini_api_key": "main"}
    assert Settings(**base).effective_embedding_api_key == "main"
    assert Settings(**base, embedding_api_key="embed").effective_embedding_api_key == "embed"
    for name, value in [("EMBEDDING_INDEXING_RPM_PERCENT", "0"), ("EMBEDDING_INDEXING_RPM_PERCENT", "101"),
                        ("EMBEDDING_INDEXING_MAX_CONCURRENCY", "0"), ("EMBEDDING_PERSISTENT_CACHE_ENABLED", "yes")]:
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            Settings.from_env()
        monkeypatch.delenv(name)
    monkeypatch.setenv("EMBEDDING_GOOGLE_API_KEY", "embed")
    monkeypatch.setenv("EMBEDDING_PERSISTENT_CACHE_ENABLED", "true")
    settings = Settings.from_env()
    assert settings.embedding_api_key == "embed" and settings.embedding_persistent_cache_enabled
    assert (settings.embedding_indexing_rpm_percent, settings.embedding_indexing_max_concurrency) == (50, 1)
# 2026-01-15 12:00 UTC is 04:00 Pacific; the Pacific day rolls over 20h later.
NOON = 1_768_478_400.0

class FakeWall:
    def __init__(self, now=NOON):
        self.now = now
    def __call__(self):
        return self.now

def test_daily_budget_counts_sent_items_only_and_warns_once(monkeypatch, caplog):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake, daily_limit=10, daily_warn_percent=50, wall_clock=FakeWall())
        try:
            await p.embed(["a", "b", "c"], "RETRIEVAL_QUERY")
            await p.embed(["a", "b", "c"], "RETRIEVAL_QUERY")
            await p.embed(["d", "e", "f"], "RETRIEVAL_QUERY")
            return p.daily.used
        finally:
            await p.aclose()
    caplog.set_level("WARNING")
    assert run(go) == 6 and len(calls) == 2
    assert caplog.text.count("embedding_daily_budget_warning") == 1

def test_daily_budget_rejects_indexing_early_but_queries_continue(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake, daily_limit=10, daily_indexing_percent=50, wall_clock=FakeWall())
        try:
            await p.embed(docs(5, "a"), "RETRIEVAL_DOCUMENT")
            with pytest.raises(RagError) as error:
                await p.embed(docs(1, "b"), "RETRIEVAL_DOCUMENT")
            assert (error.value.code, error.value.retryable, error.value.status) == (
                "RAG_EMBEDDING_QUOTA_EXHAUSTED", False, 503)
            await p.embed(docs(5, "q"), "RETRIEVAL_QUERY")
            with pytest.raises(RagError) as error:
                await p.embed(["over"], "RETRIEVAL_QUERY")
            assert error.value.code == "RAG_EMBEDDING_QUOTA_EXHAUSTED"
            return p.stats["daily_rejected"], p.daily.used
        finally:
            await p.aclose()
    assert run(go) == (2, 10) and len(calls) == 2

def test_daily_budget_resets_at_pacific_midnight(monkeypatch):
    fake = FakeTime(); install(monkeypatch, [])
    wall = FakeWall()
    async def go():
        p = provider(fake, daily_limit=2, wall_clock=wall)
        try:
            await p.embed(["a", "b"], "RETRIEVAL_QUERY")
            with pytest.raises(RagError):
                await p.embed(["c"], "RETRIEVAL_QUERY")
            assert 19 * 3600 < p.daily.seconds_until_reset() <= 20 * 3600
            wall.now += 20 * 3600
            await p.embed(["c"], "RETRIEVAL_QUERY")
            return p.daily.used
        finally:
            await p.aclose()
    assert run(go) == 1

def test_google_daily_429_closes_local_budget_until_reset(monkeypatch):
    daily = rate_limited([{"@type": QUOTA_FAILURE, "violations": [
        {"quotaId": "EmbedContentRequestsPerDayPerProjectPerModel-FreeTier"}]}])
    fake = FakeTime(); calls = install(monkeypatch, [daily])
    wall = FakeWall()
    async def go():
        p = provider(fake, daily_limit=1000, wall_clock=wall)
        try:
            with pytest.raises(RagError):
                await p.embed(["a"], "RETRIEVAL_QUERY")
            fake.now += 400  # past the 5-minute breaker
            with pytest.raises(RagError) as error:
                await p.embed(["b"], "RETRIEVAL_QUERY")
            assert error.value.code == "RAG_EMBEDDING_QUOTA_EXHAUSTED"
            wall.now += 20 * 3600
            await p.embed(["b"], "RETRIEVAL_QUERY")
        finally:
            await p.aclose()
    run(go)
    assert len(calls) == 2

def test_daily_budget_off_by_default_and_settings_validate(monkeypatch):
    fake = FakeTime(); calls = install(monkeypatch, [])
    async def go():
        p = provider(fake)
        try:
            for i in range(3):
                await p.embed([f"q{i}"], "RETRIEVAL_QUERY")
            return p.daily.enabled, p.daily.describe()
        finally:
            await p.aclose()
    assert run(go) == (False, "off") and len(calls) == 3
    assert Settings().embedding_daily_limit == 0
    for name, value in [("EMBEDDING_DAILY_LIMIT", "-1"), ("EMBEDDING_DAILY_WARN_PERCENT", "0"),
                        ("EMBEDDING_DAILY_INDEXING_PERCENT", "101")]:
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            Settings.from_env()
        monkeypatch.delenv(name)
    monkeypatch.setenv("EMBEDDING_DAILY_LIMIT", "1000")
    assert Settings.from_env().embedding_daily_limit == 1000