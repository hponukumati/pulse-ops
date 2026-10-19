"""Runtime configuration.

Secrets are never read from plain environment variables or source: each secret is a
file path (``*_FILE``) pointing at a runtime-mounted secret (Docker secret, host
secret facility). ``validate_for_startup`` rejects unsafe hosted configuration.
"""

from __future__ import annotations

import base64
import ipaddress
import json
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from urllib.parse import quote, urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(StrEnum):
    LOCAL = "local"
    TEST = "test"
    HOSTED = "hosted"


class ConfigError(RuntimeError):
    """Raised when configuration is unsafe or incomplete. Messages never contain secret values."""


# Values that must never be used as real secrets (placeholders from docs/examples).
_KNOWN_PLACEHOLDERS = {"changeme", "password", "secret", "example", "default", "pulseops"}


def _read_secret_file(path: str | None, name: str) -> bytes:
    if not path:
        raise ConfigError(f"missing secret file setting: {name}")
    p = Path(path)
    try:
        data = p.read_bytes().strip()
    except OSError as exc:
        raise ConfigError(f"secret file unreadable: {name}") from exc
    if not data:
        raise ConfigError(f"secret file empty: {name}")
    if data.decode("utf-8", "ignore").lower() in _KNOWN_PLACEHOLDERS:
        raise ConfigError(f"secret file contains a placeholder value: {name}")
    return data


def _decode_key(raw: bytes, name: str, min_bytes: int = 32) -> bytes:
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise ConfigError(f"secret is not valid base64: {name}") from exc
    if len(key) < min_bytes:
        raise ConfigError(f"secret too short: {name}")
    return key


@dataclass(frozen=True)
class Keyring:
    """Versioned HMAC keys. New digests use ``current``; old versions still verify."""

    current: int
    keys: dict[int, bytes]

    def key(self, version: int) -> bytes | None:
        return self.keys.get(version)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PULSEOPS_", extra="ignore")

    env: Env = Env.LOCAL
    debug: bool = False
    demo_mode: bool = False

    # Exact browser origins allowed for auth state changes, CORS and WebSocket handshakes.
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:8080", "http://localhost:8080"])
    # Proxy addresses whose X-Forwarded-For is trusted (the nginx frontend container).
    trusted_proxies: list[str] = Field(default_factory=list)

    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "pulseops"
    db_user: str = "pulseops_app"
    db_password_file: str | None = None
    db_maint_user: str = "pulseops_maint"
    db_maint_password_file: str | None = None
    db_migrator_user: str = "pulseops_migrator"
    db_migrator_password_file: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_timeout_s: float = 2.0
    db_ssl: bool = False

    redis_host: str = "redis"
    redis_port: int = 6379
    redis_user: str = "pulseops"
    redis_password_file: str | None = None
    redis_ssl: bool = False

    jwt_private_key_file: str | None = None
    jwt_kid: str = "k1"
    # Optional previous public key kept only to verify tokens issued before a key rotation.
    jwt_previous_public_key_file: str | None = None
    jwt_previous_kid: str | None = None
    jwt_issuer: str = "pulseops"
    jwt_audience: str = "pulseops-app"

    refresh_hmac_key_file: str | None = None
    ingest_hmac_keys_file: str | None = None  # JSON: {"current": 1, "keys": {"1": "<base64>"}}
    rate_limit_key_file: str | None = None

    deletion_ledger_dir: str = "/var/lib/pulseops/ledger"

    # Argon2id parameters (PRD minimum: 19 MiB, t=2, p=1; tuned upward).
    argon2_memory_kib: int = 47104
    argon2_time_cost: int = 2
    argon2_parallelism: int = 1
    hash_concurrency: int = 4

    # --- derived secrets (read lazily so tests can construct settings without files) ---

    @cached_property
    def db_password(self) -> str:
        return _read_secret_file(self.db_password_file, "db_password").decode()

    @cached_property
    def redis_password(self) -> str:
        return _read_secret_file(self.redis_password_file, "redis_password").decode()

    @cached_property
    def jwt_private_key_pem(self) -> bytes:
        return _read_secret_file(self.jwt_private_key_file, "jwt_private_key")

    @cached_property
    def jwt_previous_public_key_pem(self) -> bytes | None:
        if not self.jwt_previous_public_key_file:
            return None
        return _read_secret_file(self.jwt_previous_public_key_file, "jwt_previous_public_key")

    @cached_property
    def refresh_hmac_key(self) -> bytes:
        return _decode_key(_read_secret_file(self.refresh_hmac_key_file, "refresh_hmac_key"), "refresh_hmac_key")

    @cached_property
    def rate_limit_key(self) -> bytes:
        return _decode_key(_read_secret_file(self.rate_limit_key_file, "rate_limit_key"), "rate_limit_key")

    @cached_property
    def ingest_keyring(self) -> Keyring:
        raw = _read_secret_file(self.ingest_hmac_keys_file, "ingest_hmac_keys")
        try:
            doc = json.loads(raw)
            keys = {int(v): _decode_key(k.encode(), "ingest_hmac_keys") for v, k in doc["keys"].items()}
            current = int(doc["current"])
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ConfigError("ingest_hmac_keys has an invalid format") from exc
        if current not in keys:
            raise ConfigError("ingest_hmac_keys current version missing")
        return Keyring(current=current, keys=keys)

    def _db_url(self, user: str, password: str) -> str:
        return f"postgresql+asyncpg://{quote(user)}:{quote(password, safe='')}@{self.db_host}:{self.db_port}/{self.db_name}"

    def runtime_db_url(self) -> str:
        return self._db_url(self.db_user, self.db_password)

    def maint_db_url(self) -> str:
        return self._db_url(self.db_maint_user, _read_secret_file(self.db_maint_password_file, "db_maint_password").decode())

    def migrator_db_url(self) -> str:
        return self._db_url(self.db_migrator_user, _read_secret_file(self.db_migrator_password_file, "db_migrator_password").decode())

    @property
    def is_hosted(self) -> bool:
        return self.env == Env.HOSTED

    @property
    def refresh_cookie_name(self) -> str:
        # __Host- prefix requires Secure, Path=/ and no Domain: only possible over HTTPS.
        return "__Host-pulseops_refresh" if self.cookie_secure else "pulseops_refresh_dev"

    @property
    def cookie_secure(self) -> bool:
        return all(urlsplit(o).scheme == "https" for o in self.allowed_origins)


def _is_loopback_http_origin(origin: str) -> bool:
    parts = urlsplit(origin)
    if parts.scheme != "http" or not parts.hostname:
        return False
    if parts.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        return False


def validate_for_startup(settings: Settings) -> None:
    """Fail fast on unsafe configuration. Called by the API, worker and CLI at startup."""
    if not settings.allowed_origins:
        raise ConfigError("allowed_origins must not be empty")
    for origin in settings.allowed_origins:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/") or parts.query:
            raise ConfigError("allowed_origins entries must be bare scheme://host[:port] origins")
        if origin == "null" or "*" in origin:
            raise ConfigError("wildcard or null origins are not allowed")

    if settings.is_hosted:
        if settings.debug:
            raise ConfigError("debug mode is not allowed in hosted mode")
        if settings.demo_mode:
            raise ConfigError("demo mode/seeding is not allowed in hosted mode")
        if not settings.cookie_secure:
            raise ConfigError("hosted mode requires https origins and Secure cookies")
    elif not settings.cookie_secure:
        # Non-Secure dev cookie is only permitted for loopback HTTP origins.
        if not all(_is_loopback_http_origin(o) for o in settings.allowed_origins):
            raise ConfigError("http origins are only allowed on loopback in local mode")

    if settings.argon2_memory_kib < 19456 or settings.argon2_time_cost < 2 or settings.argon2_parallelism < 1:
        raise ConfigError("argon2 parameters below the required minimum")

    # Touch every required secret so missing/placeholder/short values fail at startup.
    _ = (
        settings.db_password,
        settings.redis_password,
        settings.jwt_private_key_pem,
        settings.jwt_previous_public_key_pem,
        settings.refresh_hmac_key,
        settings.rate_limit_key,
        settings.ingest_keyring,
    )
    if settings.refresh_hmac_key == settings.rate_limit_key or settings.refresh_hmac_key in settings.ingest_keyring.keys.values():
        raise ConfigError("HMAC keys must be distinct per purpose")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Used by tests and the CLI to install explicit settings."""
    global _settings
    _settings = settings
