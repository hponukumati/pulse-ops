"""JWT validation (RFC 8725 hardening) and opaque secret formats."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pulseops.security.tokens import (
    JwtService,
    TokenInvalid,
    is_well_formed_refresh,
    new_ingest_secret,
    new_refresh_secret,
    parse_ingest_token,
)


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(k: rsa.RSAPrivateKey) -> bytes:
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


KEY = _key()
SVC = JwtService(_pem(KEY), "k1", "pulseops", "pulseops-app")
UID, SID = uuid.uuid4(), uuid.uuid4()
FAR = datetime.now(UTC) + timedelta(hours=12)


def claims(**over: object) -> dict[str, object]:
    now = int(datetime.now(UTC).timestamp())
    base: dict[str, object] = {
        "iss": "pulseops",
        "aud": "pulseops-app",
        "sub": str(UID),
        "sid": str(SID),
        "typ": "access",
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "jti": "x",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def sign(c: dict[str, object], key: object = KEY, alg: str = "RS256", headers: dict[str, object] | None = None) -> str:
    return jwt.encode(c, key, algorithm=alg, headers={"kid": "k1", **(headers or {})})  # type: ignore[arg-type]


def test_round_trip() -> None:
    token, exp = SVC.issue(UID, SID, FAR)
    c = SVC.validate(token)
    assert (c.user_id, c.session_id) == (UID, SID)
    assert exp - datetime.now(UTC) <= timedelta(minutes=15)


def test_access_never_outlives_session() -> None:
    soon = datetime.now(UTC) + timedelta(minutes=3)
    _, exp = SVC.issue(UID, SID, soon)
    assert exp == soon


def _b64json(d: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_alg_none_rejected() -> None:
    with pytest.raises(TokenInvalid):
        SVC.validate(f"{_b64json({'alg': 'none', 'kid': 'k1'})}.{_b64json(claims())}.")


def test_hs256_key_confusion_rejected() -> None:
    """A token HMAC-signed with the RSA *public* key must never validate (alg confusion)."""
    public_pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    header = _b64json({"alg": "HS256", "kid": "k1", "typ": "JWT"})
    body = _b64json(claims())
    sig = base64.urlsafe_b64encode(hmac.new(public_pem, f"{header}.{body}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    with pytest.raises(TokenInvalid):
        SVC.validate(f"{header}.{body}.{sig}")


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda: sign(claims(), key=_key()),  # untrusted key
        lambda: sign(claims(), headers={"kid": "other"}),
        lambda: sign(claims(), headers={"jku": "https://attacker.example/jwks"}),
        lambda: sign(claims(), headers={"x5u": "https://attacker.example/cert"}),
        lambda: sign(claims(exp=int(datetime.now(UTC).timestamp()) - 31)),  # expired beyond skew
        lambda: sign(claims(nbf=int(datetime.now(UTC).timestamp()) + 120)),
        lambda: sign(claims(iat=int(datetime.now(UTC).timestamp()) + 120)),
        lambda: sign(claims(aud="other")),
        lambda: sign(claims(iss="other")),
        lambda: sign(claims(typ="refresh")),
        lambda: sign(claims(sid=None)),
        lambda: sign(claims(sub=None)),
        lambda: sign(claims(sub="not-a-uuid")),
        lambda: sign(claims(exp=int(datetime.now(UTC).timestamp()) + 3600)),  # lifetime > 15 min
        lambda: "garbage",
        lambda: "a" * 5000,
    ],
)
def test_invalid_tokens(token_factory) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TokenInvalid):
        SVC.validate(token_factory())


def test_skew_tolerance_is_30s() -> None:
    now = int(datetime.now(UTC).timestamp())
    SVC.validate(sign(claims(exp=now - 20, iat=now - 600, nbf=now - 600)))
    with pytest.raises(TokenInvalid):
        SVC.validate(sign(claims(exp=now - 40, iat=now - 600, nbf=now - 600)))


def test_previous_key_verifies_after_rotation() -> None:
    old = JwtService(_pem(KEY), "k1", "pulseops", "pulseops-app")
    token, _ = old.issue(UID, SID, FAR)
    new_key = _key()
    prev_pub = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    rotated = JwtService(_pem(new_key), "k2", "pulseops", "pulseops-app", prev_pub, "k1")
    assert rotated.validate(token).user_id == UID
    without_prev = JwtService(_pem(new_key), "k2", "pulseops", "pulseops-app")
    with pytest.raises(TokenInvalid):
        without_prev.validate(token)


def test_secret_formats() -> None:
    r = new_refresh_secret()
    assert is_well_formed_refresh(r) and len(base64.urlsafe_b64decode(r + "=")) == 32
    s = new_ingest_secret()
    parsed = parse_ingest_token(s.plaintext)
    assert parsed == s
    assert parse_ingest_token("poi_short_x") is None
    assert parse_ingest_token(s.plaintext + "x") is None
    token, _ = SVC.issue(UID, SID, FAR)
    assert parse_ingest_token(token) is None  # user JWTs never parse as ingest credentials
