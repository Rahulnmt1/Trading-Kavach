"""Redis cache wrapper with a no-op in-memory fallback when Redis is unavailable."""
from __future__ import annotations

import json
from typing import Any, Optional

from .config import env
from .logger import logger

try:
    import redis  # type: ignore
    _redis_available = True
except ImportError:
    _redis_available = False


class _MemoryFallback:
    """Tiny dict-backed fallback so the bot still runs without Redis."""

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self._d.get(key)

    def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        self._d[key] = value

    def delete(self, key: str) -> None:
        self._d.pop(key, None)

    def keys(self, pattern: str = "*") -> list[str]:
        if pattern == "*":
            return list(self._d.keys())
        prefix = pattern.rstrip("*")
        return [k for k in self._d.keys() if k.startswith(prefix)]

    def hset(self, key: str, field: str, value: str) -> None:
        self._d.setdefault(key, "{}")
        d = json.loads(self._d[key])
        d[field] = value
        self._d[key] = json.dumps(d)

    def hgetall(self, key: str) -> dict[str, str]:
        if key not in self._d:
            return {}
        return json.loads(self._d[key])

    def ping(self) -> bool:
        return True


class Cache:
    """Thin facade. Use `Cache.get_json` / `set_json` for typed values."""

    def __init__(self) -> None:
        self.client: Any
        if _redis_available:
            try:
                self.client = redis.Redis.from_url(env().REDIS_URL, decode_responses=True)
                self.client.ping()
                logger.info("Redis connected: {}", env().REDIS_URL)
                self._is_redis = True
                return
            except Exception as e:
                logger.warning("Redis unavailable ({}). Falling back to in-memory cache.", e)
        self.client = _MemoryFallback()
        self._is_redis = False

    def get_json(self, key: str) -> Any:
        v = self.client.get(key)
        return json.loads(v) if v else None

    def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        try:
            self.client.set(key, json.dumps(value, default=str), ex=ttl)
        except TypeError:
            self.client.set(key, json.dumps(value, default=str))

    def hset_json(self, key: str, field: str, value: Any) -> None:
        self.client.hset(key, field, json.dumps(value, default=str))

    def hgetall_json(self, key: str) -> dict[str, Any]:
        raw = self.client.hgetall(key)
        return {k: json.loads(v) for k, v in raw.items()} if raw else {}

    def delete(self, key: str) -> None:
        self.client.delete(key)

    def keys(self, pattern: str = "*") -> list[str]:
        return list(self.client.keys(pattern))


_cache: Optional[Cache] = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache
