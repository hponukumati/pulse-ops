"""Shared, atomic rate limits backed by Redis.

All limits are evaluated in Lua so checks across several buckets are all-or-nothing
and consistent across processes. Redis server time is used to avoid client clock skew.
If Redis is unavailable callers decide whether to fail closed (ingest, login, refresh,
mutations) or use the stricter local fallback (authenticated reads).
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from pulseops.errors import DependencyUnavailable, RateLimited

# KEYS: bucket keys. ARGV: for each key (rate_per_sec, burst, cost).
# Returns {1, 0} if allowed (and consumed), otherwise {0, retry_after_ms}.
_BUCKET_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local n = #KEYS
local levels = {}
local wait = 0
for i = 1, n do
  local rate = tonumber(ARGV[(i - 1) * 3 + 1])
  local burst = tonumber(ARGV[(i - 1) * 3 + 2])
  local cost = tonumber(ARGV[(i - 1) * 3 + 3])
  if cost > burst then return {0, -1} end
  local d = redis.call('HMGET', KEYS[i], 'tok', 'ts')
  local tok = tonumber(d[1])
  local ts = tonumber(d[2])
  if tok == nil or ts == nil then tok = burst; ts = now end
  tok = math.min(burst, tok + math.max(0, now - ts) * rate / 1000)
  if tok < cost then
    local w = math.ceil((cost - tok) * 1000 / rate)
    if w > wait then wait = w end
  end
  levels[i] = tok
end
if wait > 0 then return {0, wait} end
for i = 1, n do
  local rate = tonumber(ARGV[(i - 1) * 3 + 1])
  local burst = tonumber(ARGV[(i - 1) * 3 + 2])
  local cost = tonumber(ARGV[(i - 1) * 3 + 3])
  redis.call('HSET', KEYS[i], 'tok', tostring(levels[i] - cost), 'ts', tostring(now))
  redis.call('PEXPIRE', KEYS[i], math.ceil(burst * 1000 / rate) + 1000)
end
return {1, 0}
"""

# Fixed-window counter. KEYS[1]: counter. ARGV: limit, window_ms, increment(0/1).
# Returns {allowed, retry_after_ms, count}.
_WINDOW_LUA = """
local c = tonumber(redis.call('GET', KEYS[1]) or '0')
local ttl = redis.call('PTTL', KEYS[1])
if c >= tonumber(ARGV[1]) then
  if ttl < 0 then ttl = tonumber(ARGV[2]) end
  return {0, ttl, c}
end
if tonumber(ARGV[3]) == 1 then
  c = redis.call('INCR', KEYS[1])
  if c == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
end
return {1, 0, c}
"""

# Concurrency leases. KEYS: zsets. ARGV: member, lease_ms, then limit per key.
# Returns 1 when a lease was acquired (or refreshed, if the member already holds one) in
# every set, 0 otherwise. Expired leases are dropped first, so crashed holders free slots.
_LEASE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local member = ARGV[1]
local lease = tonumber(ARGV[2])
for i = 1, #KEYS do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now)
  if not redis.call('ZSCORE', KEYS[i], member) and redis.call('ZCARD', KEYS[i]) >= tonumber(ARGV[2 + i]) then return 0 end
end
for i = 1, #KEYS do
  redis.call('ZADD', KEYS[i], now + lease, member)
  redis.call('PEXPIRE', KEYS[i], lease * 2)
end
return 1
"""


@dataclass(frozen=True)
class Bucket:
    key: str
    rate_per_sec: float
    burst: int
    cost: int = 1


def make_redis(host: str, port: int, username: str | None, password: str | None, *, use_ssl: bool = False, timeout: float = 0.5) -> Redis:
    # No automatic retries: an outage must fail fast so callers can fail closed with 503.
    return Redis(
        host=host,
        port=port,
        username=username,
        password=password,
        ssl=use_ssl,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        retry=Retry(NoBackoff(), 0),
        health_check_interval=10,
    )


class RateLimiter:
    def __init__(self, redis: Redis, key_secret: bytes) -> None:
        self.redis = redis
        self._key_secret = key_secret
        self._bucket = redis.register_script(_BUCKET_LUA)
        self._window = redis.register_script(_WINDOW_LUA)
        self._lease = redis.register_script(_LEASE_LUA)

    def opaque(self, value: str) -> str:
        """Keyed hash for identifiers (e.g. client IPs, emails) so raw values never sit in Redis."""
        return hmac.new(self._key_secret, value.encode(), hashlib.sha256).hexdigest()[:32]

    async def consume(self, buckets: list[Bucket]) -> None:
        """Atomically consume from every bucket or raise RateLimited with Retry-After."""
        args: list[float | int] = []
        for b in buckets:
            args += [b.rate_per_sec, b.burst, b.cost]
        try:
            allowed, wait_ms = await self._bucket(keys=[b.key for b in buckets], args=args)
        except (RedisError, OSError) as exc:
            raise DependencyUnavailable() from exc
        if not allowed:
            raise RateLimited(retry_after=max(1, -(-int(wait_ms) // 1000)) if int(wait_ms) > 0 else 60)

    async def window_check(self, key: str, limit: int, window_s: int, *, increment: bool) -> None:
        try:
            allowed, ttl_ms, _ = await self._window(keys=[key], args=[limit, window_s * 1000, 1 if increment else 0])
        except (RedisError, OSError) as exc:
            raise DependencyUnavailable() from exc
        if not allowed:
            raise RateLimited(retry_after=max(1, -(-int(ttl_ms) // 1000)))

    async def window_increment(self, key: str, window_s: int) -> None:
        try:
            async with self.redis.pipeline(transaction=True) as p:
                p.incr(key)
                p.expire(key, window_s, nx=True)
                await p.execute()
        except (RedisError, OSError) as exc:
            raise DependencyUnavailable() from exc

    async def acquire_lease(self, keys_limits: list[tuple[str, int]], lease_ms: int = 10_000, member: str | None = None) -> str:
        member = member or uuid.uuid4().hex
        try:
            ok = await self._lease(keys=[k for k, _ in keys_limits], args=[member, lease_ms, *[lim for _, lim in keys_limits]])
        except (RedisError, OSError) as exc:
            raise DependencyUnavailable() from exc
        if not ok:
            raise RateLimited(retry_after=1)
        return member

    async def release_lease(self, keys: list[str], member: str) -> None:
        try:
            async with self.redis.pipeline(transaction=False) as p:
                for k in keys:
                    p.zrem(k, member)
                await p.execute()
        except (RedisError, OSError):
            pass  # leases expire on their own


class LocalFallbackLimiter:
    """Stricter in-process limit (60/min/user) used for reads only while Redis is down."""

    def __init__(self, per_minute: int = 60) -> None:
        self.rate = per_minute / 60.0
        self.burst = per_minute
        self._state: dict[str, tuple[float, float]] = {}

    def consume(self, key: str) -> None:
        now = time.monotonic()
        tokens, ts = self._state.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - ts) * self.rate)
        if tokens < 1:
            raise RateLimited(retry_after=max(1, int((1 - tokens) / self.rate) + 1))
        self._state[key] = (tokens - 1, now)
        if len(self._state) > 10_000:  # bounded memory
            self._state.clear()
