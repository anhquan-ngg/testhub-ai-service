"""Adapter that gives the provider a TTL-bound view of the repository's durable query-vector cache."""

class RepositoryEmbeddingCache:
    def __init__(self, repository, ttl_days: int) -> None:
        self.repository = repository
        self.ttl_days = ttl_days

    async def get_many(self, keys: list[str]) -> dict[str, list[float]]:
        return await self.repository.cache_get_many(keys, self.ttl_days)

    async def put_many(self, items: dict[str, list[float]]) -> None:
        await self.repository.cache_put_many(items)
