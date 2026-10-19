"""Access JWTs (RS256, pinned keys) and opaque refresh/ingest secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from pulseops.config import Keyring

ACCESS_TTL = timedelta(minutes=15)
CLOCK_SKEW_S = 30
_FORBIDDEN_HEADERS = ("jku", "x5u", "jwk", "x5c", "crit")
_REQUIRED_CLAIMS = ["iss", "aud", "sub", "sid", "typ", "iat", "nbf", "exp", "jti"]


class TokenInvalid(Exception):
    """Raised for any invalid token; carries no detail to callers."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime


class JwtService:
    """Issues and validates access JWTs. Only RS256 and only locally pinned keys are accepted."""

    def __init__(
        self,
        private_key_pem: bytes,
        kid: str,
        issuer: str,
        audience: str,
        previous_public_key_pem: bytes | None = None,
        previous_kid: str | None = None,
    ) -> None:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("JWT signing key must be RSA >= 2048 bits")
        self._private = key
        self._kid = kid
        self._issuer = issuer
        self._audience = audience
        self._public: dict[str, RSAPublicKey] = {kid: key.public_key()}
        if previous_public_key_pem and previous_kid:
            prev = serialization.load_pem_public_key(previous_public_key_pem)
            if not isinstance(prev, RSAPublicKey):
                raise ValueError("previous JWT key must be RSA")
            self._public[previous_kid] = prev

    def issue(
        self, user_id: uuid.UUID, session_id: uuid.UUID, session_expires_at: datetime, now: datetime | None = None
    ) -> tuple[str, datetime]:
        now = now or datetime.now(UTC)
        exp = min(now + ACCESS_TTL, session_expires_at)
        claims = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": str(user_id),
            "sid": str(session_id),
            "typ": "access",
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "jti": uuid.uuid4().hex,
        }
        token = jwt.encode(claims, self._private, algorithm="RS256", headers={"kid": self._kid, "typ": "JWT"})
        return token, exp

    def validate(self, token: str) -> AccessClaims:
        try:
            if len(token) > 4096:
                raise TokenInvalid()
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or any(h in header for h in _FORBIDDEN_HEADERS):
                raise TokenInvalid()
            key = self._public.get(str(header.get("kid")))
            if key is None:
                raise TokenInvalid()
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=CLOCK_SKEW_S,
                options={"require": _REQUIRED_CLAIMS, "strict_aud": True},
            )
            if claims["typ"] != "access":
                raise TokenInvalid()
            if int(claims["exp"]) - int(claims["iat"]) > ACCESS_TTL.total_seconds():
                raise TokenInvalid()
            return AccessClaims(
                user_id=uuid.UUID(str(claims["sub"])),
                session_id=uuid.UUID(str(claims["sid"])),
                expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
            )
        except TokenInvalid:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise TokenInvalid() from exc


# ---- Opaque secrets -------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def keyed_digest(key: bytes, secret: str) -> bytes:
    return hmac.new(key, secret.encode(), hashlib.sha256).digest()


def new_refresh_secret() -> str:
    """256 random bits. Only a keyed digest is persisted."""
    return _b64(secrets.token_bytes(32))


_REFRESH_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def is_well_formed_refresh(secret: str) -> bool:
    return bool(_REFRESH_RE.match(secret))


# Ingest token format: "poi_<public_id>_<secret>" where public_id is 20 base32 chars and
# secret is 256 random bits (43 base64url chars). The public id is safe to display.
_INGEST_RE = re.compile(r"^poi_([a-z2-7]{20})_([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True)
class IngestSecret:
    public_id: str
    secret: str

    @property
    def plaintext(self) -> str:
        return f"poi_{self.public_id}_{self.secret}"


def new_ingest_secret() -> IngestSecret:
    public_id = base64.b32encode(secrets.token_bytes(15)).decode().lower().rstrip("=")[:20]
    return IngestSecret(public_id=public_id, secret=_b64(secrets.token_bytes(32)))


def parse_ingest_token(token: str) -> IngestSecret | None:
    m = _INGEST_RE.match(token)
    return IngestSecret(public_id=m.group(1), secret=m.group(2)) if m else None


def ingest_digest(keyring: Keyring, version: int, secret: str) -> bytes | None:
    key = keyring.key(version)
    return keyed_digest(key, secret) if key else None


def constant_time_equal(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)
