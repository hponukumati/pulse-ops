"""Argon2id password hashing and password policy.

Hashing runs in a bounded thread pool so concurrent logins cannot exhaust CPU/RAM.
"""

from __future__ import annotations

import asyncio
import unicodedata
from functools import lru_cache
from importlib import resources

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

MIN_LENGTH = 15
MAX_LENGTH = 128


def normalize(password: str) -> str:
    """Consistent Unicode normalization (NFKC) applied before hashing and verification."""
    return unicodedata.normalize("NFKC", password)


@lru_cache(maxsize=1)
def _blocklist() -> frozenset[str]:
    text = resources.files("pulseops.data").joinpath("common_passwords.txt").read_text(encoding="utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip() and not line.startswith("#"))


def policy_violations(password: str, *, email: str | None = None) -> list[str]:
    """Return policy violation codes. No composition rules; no truncation."""
    pw = normalize(password)
    codes: list[str] = []
    if len(pw) < MIN_LENGTH:
        codes.append("too_short")
    if len(pw) > MAX_LENGTH:
        codes.append("too_long")
    lowered = pw.lower()
    if lowered in _blocklist() or len(set(pw)) <= 2:
        codes.append("too_common")
    if email and lowered == email.lower():
        codes.append("matches_email")
    return codes


class PasswordHasher:
    def __init__(self, memory_kib: int, time_cost: int, parallelism: int, concurrency: int) -> None:
        self._hash = PasswordHash((Argon2Hasher(time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism),))
        self._sem = asyncio.Semaphore(concurrency)
        # Dummy hash verified for unknown users so response timing does not reveal account existence.
        self._dummy = self._hash.hash("pulseops-dummy-password-for-timing-equalization")

    async def hash(self, password: str) -> str:
        async with self._sem:
            return await asyncio.to_thread(self._hash.hash, normalize(password))

    async def verify(self, password: str, encoded: str | None) -> bool:
        if len(password) > MAX_LENGTH * 4:  # reject absurd inputs before hashing work
            encoded = None
        async with self._sem:
            target = encoded or self._dummy
            ok = await asyncio.to_thread(self._safe_verify, normalize(password), target)
        return ok and encoded is not None

    def _safe_verify(self, password: str, encoded: str) -> bool:
        try:
            return self._hash.verify(password, encoded)
        except Exception:
            return False
