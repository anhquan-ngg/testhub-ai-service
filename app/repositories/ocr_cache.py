"""TTL-bound view of the repository's durable OCR transcript cache."""

class RepositoryOcrCache:
    def __init__(self, repository, ttl_days: int) -> None:
        self.repository = repository
        self.ttl_days = ttl_days

    async def get_many(self, keys: list[str]) -> dict[str, str]:
        return await self.repository.ocr_cache_get_many(keys, self.ttl_days)

    async def put_many(self, items: dict[str, str]) -> None:
        await self.repository.ocr_cache_put_many(items)
