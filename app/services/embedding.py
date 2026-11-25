"""Independent Google embedding adapter; uses EMBEDDING_GOOGLE_API_KEY (else GEMINI_API_KEY), not LLM retries.

One provider instance lives per process (FastAPI lifespan), so its limiter, cache
and counters are process-wide. Every outbound call passes through the limiter;
transient 429/5xx responses are retried with bounded, jittered backoff that honors
Retry-After/RetryInfo, while daily-quota and configuration errors fail fast.
"""

import asyncio
import hashlib
import logging
import math
import random
import time
from array import array
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from datetime import time as dt_time
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from google import genai
from google.genai import errors, types

from app.schemas.rag import BATCH_SIZE, DIMENSIONS, MAX_CHUNK_BYTES
from app.services.rag_errors import RagError

logger = logging.getLogger(__name__)

MODEL = "gemini-embedding-001"
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
CONFIG_STATUSES = frozenset({400, 401, 403, 404})
BASE_BACKOFF_S = 1.0
QUOTA_BREAKER_S = 300.0
INDEXING_YIELD_S = 0.25
PERSISTENT_CACHE_TIMEOUT_S = 2.0

Task = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]

@dataclass
class EmbeddingUsage:
    """Per-call billing evidence; unknown adapters conservatively keep the reservation."""

    upstream_started: bool = True

@dataclass
class _RequestProgress:
    """Invocation-local send evidence retained on failure and cancellation."""

    sends: int = 0

class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str], task: Task) -> list[list[float]]: ...
    async def aclose(self) -> None: ...

class PersistentEmbeddingCache(Protocol):
    """Durable query-vector store keyed by the provider's sha256 key (never raw text)."""

    async def get_many(self, keys: list[str]) -> dict[str, list[float]]: ...
    async def put_many(self, items: dict[str, list[float]]) -> None: ...

def normalized_vector(values: list[float]) -> list[float]:
    """Reject malformed, nonfinite, zero or wrong-size vectors before persistence."""
    if len(values) != DIMENSIONS or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in values):
        raise RagError("RAG_EMBEDDING_INVALID", 502)
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm <= 0:
        raise RagError("RAG_EMBEDDING_INVALID", 502)
    return [value / norm for value in values]

def _error_details(exc: errors.APIError) -> list[dict[str, Any]]:
    """Return google.rpc detail objects without touching message text."""
    body = exc.details if isinstance(exc.details, dict) else {}
    error = body.get("error", body)
    details = error.get("details") if isinstance(error, dict) else None
    return [item for item in details if isinstance(item, dict)] if isinstance(details, list) else []

def server_retry_delay(exc: errors.APIError, *, now: float | None = None) -> float | None:
    """Seconds requested by Retry-After (seconds or HTTP date) or RetryInfo."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    value = headers.get("retry-after") if headers is not None else None
    if value is not None:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = max(0.0, parsed.timestamp() - (time.time() if now is None else now))
            except (TypeError, ValueError, OverflowError, IndexError):
                seconds = float("nan")
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    for item in _error_details(exc):
        if str(item.get("@type", "")).endswith("RetryInfo"):
            delay = item.get("retryDelay")
            try:
                seconds = float(str(delay).removesuffix("s"))
            except ValueError:
                continue
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
    return None

def is_daily_quota(exc: errors.APIError) -> bool:
    """True when a QuotaFailure names a per-day quota; waiting seconds cannot help."""
    for item in _error_details(exc):
        if str(item.get("@type", "")).endswith("QuotaFailure"):
            for violation in item.get("violations") or []:
                if isinstance(violation, dict) and any(
                    "PerDay" in str(violation.get(key, "")) for key in ("quotaId", "quotaMetric")):
                    return True
    return False

class RateLimiter:
    """Process-wide sliding-window item budget plus a shared 429 cool-down.

    The budget counts input items, not HTTP requests, because Google does not
    document whether batch items count individually; this errs on the safe side.
    """

    def __init__(self, per_minute: int | None, *, clock: Callable[[], float],
                 sleep: Callable[[float], Awaitable[None]], background_share: float = 1.0) -> None:
        self.per_minute, self._clock, self._sleep = per_minute, clock, sleep
        self._window: deque[tuple[float, int, bool]] = deque()
        self._used = self._background_used = 0
        self._paused_until = 0.0
        # Background (indexing) work may use at most this slice of the window so queries always fit.
        self.background_cap = (
            None if per_minute is None or background_share >= 1.0
            else min(per_minute, max(BATCH_SIZE, int(per_minute * background_share)))
        )

    def pause(self, seconds: float) -> None:
        """Make every caller wait after an upstream 429 instead of hammering."""
        self._paused_until = max(self._paused_until, self._clock() + seconds)

    def _wait_needed(self, cost: int, background: bool = False) -> float:
        if cost < 1 or (self.per_minute is not None and cost > self.per_minute):
            raise RagError("RAG_EMBEDDING_INPUT_INVALID")
        now = self._clock()
        while self._window and self._window[0][0] <= now - 60:
            _, expired, was_background = self._window.popleft()
            self._used -= expired
            self._background_used -= expired if was_background else 0
        wait = max(0.0, self._paused_until - now)
        if self.per_minute is not None and self._used + cost > self.per_minute:
            wait = max(wait, self._window[0][0] + 60 - now)
        if background and self.background_cap is not None and self._background_used + cost > self.background_cap:
            oldest = next((entry[0] for entry in self._window if entry[2]), now)
            wait = max(wait, oldest + 60 - now)
        return wait

    def charge(self, cost: int, background: bool = False) -> None:
        """Charge immediately before send, with no await after checking admission."""
        if self._wait_needed(cost, background) > 0:
            raise RagError("RAG_EMBEDDING_RATE_LIMITED", 503, True)
        self._window.append((self._clock(), cost, background))
        self._used += cost
        self._background_used += cost if background else 0

    async def acquire(self, cost: int, max_wait: float, background: bool = False) -> float:
        """Reserve `cost` items; raise a retryable error instead of exceeding max_wait."""
        waited = 0.0
        while (wait := self._wait_needed(cost, background)) > 0:
            if waited + wait > max_wait:
                raise RagError("RAG_EMBEDDING_RATE_LIMITED", 503, True)
            await self._sleep(wait)
            waited += wait
        self.charge(cost, background)
        return waited

class DailyBudget:
    """Process-local requests-per-day guard that fails early with a clear quota error.

    Google resets the daily quota at midnight Pacific time (assumed, not guaranteed). The
    counter lives in memory, so it resets on restart and each replica needs its own share.
    """

    RESET_ZONE = "America/Los_Angeles"

    def __init__(self, limit: int, *, warn_percent: int = 80, indexing_percent: int = 80,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self.limit = max(0, limit)
        self._warn_at = max(1, self.limit * warn_percent // 100)
        self._background_cap = max(1, self.limit * indexing_percent // 100)
        self._wall_clock = wall_clock
        try:
            self._tz: tzinfo = ZoneInfo(self.RESET_ZONE)
        except ZoneInfoNotFoundError:
            self._tz = timezone(timedelta(hours=-8))
        self.used = 0
        self._day: date | None = None
        self._warned = self._exhausted = False

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def _roll(self) -> datetime:
        now = datetime.fromtimestamp(self._wall_clock(), self._tz)
        if self._day != now.date():
            self._day, self.used, self._warned, self._exhausted = now.date(), 0, False, False
        return now

    def seconds_until_reset(self) -> int:
        now = self._roll()
        midnight = datetime.combine(now.date() + timedelta(days=1), dt_time(), tzinfo=self._tz)
        return max(0, int((midnight - now).total_seconds()))

    def admit(self, cost: int, background: bool) -> bool:
        """Count `cost` items before a send; False means reject (indexing stops earlier than queries)."""
        if not self.enabled:
            return True
        self._roll()
        cap = self._background_cap if background else self.limit
        if self._exhausted or self.used + cost > cap:
            return False
        self.used += cost
        if self.used >= self._warn_at and not self._warned:
            self._warned = True
            logger.warning("embedding_daily_budget_warning used=%s limit=%s resets_in_s=%s",
                           self.used, self.limit, self.seconds_until_reset())
        return True

    def mark_exhausted(self) -> None:
        """Google reported the daily quota is gone; stay closed until the next reset."""
        if self.enabled:
            self._roll()
            self._exhausted = True

    def describe(self) -> str:
        return f"{self.used}/{self.limit}" if self.enabled else "off"

class GoogleEmbeddingProvider:
    def __init__(
        self, api_key: str, *, max_rpm: int | None = None, max_concurrency: int | None = None,
        max_attempts: int = 1, max_backoff_ms: int = 8_000, retry_budget_ms: int = 15_000,
        cache_size: int = 2_048, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic, rng: Callable[[], float] = random.random,
        wall_clock: Callable[[], float] = time.time, indexing_rpm_share: float = 1.0,
        indexing_max_concurrency: int | None = None, daily_limit: int = 0,
        daily_warn_percent: int = 80, daily_indexing_percent: int = 80,
    ) -> None:
        try:
            self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
                timeout=25_000, retry_options=types.HttpRetryOptions(attempts=1),
            ))
        except Exception:
            raise RagError("RAG_EMBEDDING_UNAVAILABLE", 503) from None
        self._sleep, self._clock, self._rng, self._wall_clock = sleep, clock, rng, wall_clock
        self.max_attempts = max(1, max_attempts)
        self.max_backoff = max_backoff_ms / 1000
        self.retry_budget = retry_budget_ms / 1000
        self.limiter = RateLimiter(max_rpm, clock=clock, sleep=sleep, background_share=indexing_rpm_share)
        self.daily = DailyBudget(daily_limit, warn_percent=daily_warn_percent,
                                 indexing_percent=daily_indexing_percent, wall_clock=wall_clock)
        self._slots = asyncio.Semaphore(max_concurrency) if max_concurrency else None
        self._indexing_slots = asyncio.Semaphore(indexing_max_concurrency) if indexing_max_concurrency else None
        self._interactive = 0  # in-flight query embeds; indexing yields while this is positive
        self._quota_open_until = float("-inf")
        self._cache: OrderedDict[str, array] = OrderedDict()
        self._cache_size = max(0, cache_size)
        self.stats = dict.fromkeys(("calls", "items", "cache_hits", "deduplicated", "api_requests",
            "api_items", "retries", "rate_limited", "quota_exhausted", "throttled", "failures", "indexing_yields",
            "persistent_hits", "persistent_errors", "daily_rejected"), 0)
        self._persistent: PersistentEmbeddingCache | None = None

    @staticmethod
    def _key(task: str, text: str) -> str:
        return hashlib.sha256(f"{MODEL}\0{DIMENSIONS}\0{task}\0{text}".encode()).hexdigest()

    def _cache_get(self, key: str) -> array | None:
        vector = self._cache.get(key)
        if vector is not None:
            self._cache.move_to_end(key)
        return vector

    def _cache_put(self, key: str, vector: list[float]) -> array:
        stored = array("d", vector)
        if self._cache_size:
            self._cache[key] = stored
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return stored

    def attach_persistent_cache(self, store: "PersistentEmbeddingCache | None") -> None:
        """Enable the durable query-vector cache (L2); it is best-effort and never required."""
        self._persistent = store

    async def _persistent_get(self, keys: list[str]) -> dict[str, list[float]]:
        try:
            return await asyncio.wait_for(self._persistent.get_many(keys), PERSISTENT_CACHE_TIMEOUT_S)
        except Exception:
            self.stats["persistent_errors"] += 1
            logger.warning("embedding_persistent_cache_unavailable op=get")
            return {}

    async def _persistent_put(self, items: dict[str, list[float]]) -> None:
        try:
            await asyncio.wait_for(self._persistent.put_many(items), PERSISTENT_CACHE_TIMEOUT_S)
        except Exception:
            self.stats["persistent_errors"] += 1
            logger.warning("embedding_persistent_cache_unavailable op=put")

    async def embed(self, texts: list[str], task: Task, *, usage: EmbeddingUsage | None = None) -> list[list[float]]:
        """Normalize vectors and optionally report whether any upstream send started."""
        if usage is not None:
            usage.upstream_started = False
        if not 1 <= len(texts) <= BATCH_SIZE or any(not s.strip() or len(s.encode()) > MAX_CHUNK_BYTES for s in texts):
            raise RagError("RAG_EMBEDDING_INPUT_INVALID")
        started = self._clock()
        keys = [self._key(task, text) for text in texts]
        found: dict[str, array] = {}
        pending: dict[str, str] = {}
        for key, text in zip(keys, texts, strict=True):
            if key in found or key in pending:
                continue
            if (cached := self._cache_get(key)) is not None:
                found[key] = cached
            else:
                pending[key] = text
        l2_hits = 0
        if pending and task == "RETRIEVAL_QUERY" and self._persistent is not None:
            for key, vector in (await self._persistent_get(list(pending))).items():
                found[key] = self._cache_put(key, vector)
                pending.pop(key, None)
                l2_hits += 1
        hits, unique = len(found), len(found) + len(pending)
        self.stats["persistent_hits"] += l2_hits
        self.stats["calls"] += 1
        self.stats["items"] += len(texts)
        self.stats["cache_hits"] += hits
        self.stats["deduplicated"] += len(texts) - unique
        outcome = "ok" if pending else "cached"
        progress = _RequestProgress()
        interactive = int(task == "RETRIEVAL_QUERY" and bool(pending))
        self._interactive += interactive
        try:
            if pending:
                vectors, _ = await self._request(list(pending.values()), task, usage, progress=progress)
                for key, vector in zip(pending, vectors, strict=True):
                    found[key] = self._cache_put(key, vector)
                if task == "RETRIEVAL_QUERY" and self._persistent is not None:
                    await self._persistent_put(dict(zip(pending, vectors, strict=True)))
            return [list(found[key]) for key in keys]
        except RagError as exc:
            outcome = exc.code
            self.stats["failures"] += 1
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self._interactive -= interactive
            logger.info(
                "embedding_call task=%s items=%s unique=%s cache_hits=%s api_items=%s attempts=%s outcome=%s daily=%s latency_ms=%s",
                task, len(texts), unique, hits, len(pending), progress.sends, outcome, self.daily.describe(),
                round((self._clock() - started) * 1000),
            )

    async def _request(
        self, texts: list[str], task: Task, usage: EmbeddingUsage | None = None,
        *, progress: _RequestProgress | None = None,
    ) -> tuple[list[list[float]], int]:
        """Charge at send under concurrency admission; recheck shared state after waits."""
        if progress is None:
            progress = _RequestProgress()
        slept, attempt = 0.0, 0
        background = task == "RETRIEVAL_DOCUMENT"
        while True:
            if self._clock() < self._quota_open_until:
                raise RagError("RAG_EMBEDDING_QUOTA_EXHAUSTED", 503)
            try:
                while True:
                    if background and self._indexing_slots is not None:
                        await self._indexing_slots.acquire()
                    try:
                        if self._slots is not None:
                            await self._slots.acquire()
                        try:
                            if self._clock() < self._quota_open_until:
                                raise RagError("RAG_EMBEDDING_QUOTA_EXHAUSTED", 503)
                            wait = self.limiter._wait_needed(len(texts), background)
                            if background and self._interactive:
                                # Interactive generation has priority: indexing only polls while queries run.
                                self.stats["indexing_yields"] += 1
                                wait = max(wait, INDEXING_YIELD_S)
                            if wait <= 0:
                                if not self.daily.admit(len(texts), background):
                                    self.stats["daily_rejected"] += 1
                                    logger.warning(
                                        "embedding_daily_budget_rejected task=%s used=%s limit=%s resets_in_s=%s",
                                        task, self.daily.used, self.daily.limit, self.daily.seconds_until_reset())
                                    raise RagError("RAG_EMBEDDING_QUOTA_EXHAUSTED", 503)
                                self.limiter.charge(len(texts), background)
                                attempt += 1
                                self.stats["api_requests"] += 1
                                self.stats["api_items"] += len(texts)
                                if usage is not None:
                                    # Never refund once a send may have reached the provider, even on retry/cancel.
                                    usage.upstream_started = True
                                progress.sends += 1
                                response = await self._send(texts, task)
                                break
                        finally:
                            if self._slots is not None:
                                self._slots.release()
                    finally:
                        if background and self._indexing_slots is not None:
                            self._indexing_slots.release()
                    if slept + wait > self.retry_budget:
                        self.stats["throttled"] += 1
                        raise RagError("RAG_EMBEDDING_RATE_LIMITED", 503, True)
                    await self._sleep(wait)
                    slept += wait
                if not response.embeddings or len(response.embeddings) != len(texts):
                    raise RagError("RAG_EMBEDDING_INVALID", 502)
                return [normalized_vector(item.values or []) for item in response.embeddings], attempt
            except RagError:
                raise
            except (TimeoutError, httpx.TimeoutException):
                # A timed-out call may already have consumed quota; the caller's deadline decides.
                raise RagError("RAG_EMBEDDING_TIMEOUT", 504, True) from None
            except Exception as exc:
                status = exc.code if isinstance(exc, errors.APIError) else None
                logger.warning("embedding_failed exception_type=%s status=%s attempt=%s", type(exc).__name__, status, attempt)
                if status in CONFIG_STATUSES:
                    raise RagError("RAG_EMBEDDING_CONFIG", 502) from None
                if status == 429:
                    self.stats["rate_limited"] += 1
                    if is_daily_quota(exc):
                        self.stats["quota_exhausted"] += 1
                        self._quota_open_until = self._clock() + QUOTA_BREAKER_S
                        self.daily.mark_exhausted()
                        raise RagError("RAG_EMBEDDING_QUOTA_EXHAUSTED", 503) from None
                final = RagError("RAG_EMBEDDING_RATE_LIMITED" if status == 429 else "RAG_EMBEDDING_UNAVAILABLE", 503, True)
                if status not in RETRYABLE_STATUSES and not isinstance(exc, httpx.TransportError):
                    raise final from None
                server = server_retry_delay(exc, now=self._wall_clock()) if isinstance(exc, errors.APIError) else None
                ceiling = min(self.max_backoff, BASE_BACKOFF_S * 2 ** (attempt - 1))
                delay = server if server is not None else ceiling / 2 + self._rng() * ceiling / 2
                if status == 429:
                    # Shared cool-down: concurrent callers back off too; the next acquire() waits it out.
                    self.limiter.pause(delay)
                if attempt >= self.max_attempts or delay > self.max_backoff or slept + delay > self.retry_budget:
                    raise final from None
                self.stats["retries"] += 1
                logger.info("embedding_retry status=%s attempt=%s delay_ms=%s source=%s",
                            status, attempt, round(delay * 1000), "server" if server is not None else "backoff")
                if status != 429:
                    await self._sleep(delay)
                    slept += delay

    async def _send(self, texts: list[str], task: Task) -> types.EmbedContentResponse:
        return await asyncio.wait_for(self._client.aio.models.embed_content(
            model=MODEL, contents=texts,
            config=types.EmbedContentConfig(task_type=task, output_dimensionality=DIMENSIONS),
        ), timeout=30)

    async def aclose(self) -> None:
        """Close independently without letting SDK diagnostics escape shutdown."""
        try:
            await asyncio.wait_for(self._client.aio.aclose(), 5)
        except Exception:
            logger.warning("embedding_async_close_failed")
        try:
            self._client.close()
        except Exception:
            logger.warning("embedding_sync_close_failed")
