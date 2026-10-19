"""Unsafe configuration rejection, allowlisted logging and password policy."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import secrets
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pulseops.config import ConfigError, Env, Settings, validate_for_startup
from pulseops.logging import AllowlistJsonFormatter, log_event
from pulseops.security.passwords import PasswordHasher, policy_violations


@pytest.fixture
def secret_files(tmp_path: Path) -> dict[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "jwt.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    for n in ("refresh", "ratelimit"):
        (tmp_path / n).write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    (tmp_path / "ingest.json").write_text(json.dumps({"current": 1, "keys": {"1": base64.b64encode(secrets.token_bytes(32)).decode()}}))
    (tmp_path / "pw").write_text(secrets.token_hex(16))
    return {
        "db_password_file": str(tmp_path / "pw"),
        "redis_password_file": str(tmp_path / "pw"),
        "jwt_private_key_file": str(tmp_path / "jwt.pem"),
        "refresh_hmac_key_file": str(tmp_path / "refresh"),
        "rate_limit_key_file": str(tmp_path / "ratelimit"),
        "ingest_hmac_keys_file": str(tmp_path / "ingest.json"),
    }


def hosted(files: dict[str, str], **over: object) -> Settings:
    return Settings(**{"env": Env.HOSTED, "allowed_origins": ["https://pulseops.example"], **files, **over})  # type: ignore[arg-type]


def test_hosted_valid(secret_files: dict[str, str]) -> None:
    s = hosted(secret_files)
    validate_for_startup(s)
    assert s.refresh_cookie_name == "__Host-pulseops_refresh" and s.cookie_secure


@pytest.mark.parametrize(
    "over",
    [
        {"debug": True},
        {"demo_mode": True},
        {"allowed_origins": ["http://pulseops.example"]},
        {"allowed_origins": ["https://pulseops.example", "http://127.0.0.1:8080"]},
        {"allowed_origins": ["*"]},
        {"allowed_origins": ["null"]},
        {"allowed_origins": ["https://pulseops.example/path"]},
        {"argon2_memory_kib": 8192},
        {"argon2_time_cost": 1},
        {"jwt_private_key_file": None},
        {"refresh_hmac_key_file": "/nonexistent"},
    ],
)
def test_hosted_rejects_unsafe(secret_files: dict[str, str], over: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        validate_for_startup(hosted(secret_files, **over))


def test_placeholder_and_short_secrets_rejected(secret_files: dict[str, str], tmp_path: Path) -> None:
    (tmp_path / "placeholder").write_text("changeme")
    with pytest.raises(ConfigError):
        validate_for_startup(hosted(secret_files, db_password_file=str(tmp_path / "placeholder")))
    (tmp_path / "short").write_text(base64.b64encode(b"short").decode())
    with pytest.raises(ConfigError):
        validate_for_startup(hosted(secret_files, refresh_hmac_key_file=str(tmp_path / "short")))


def test_keys_must_differ(secret_files: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        validate_for_startup(hosted(secret_files, rate_limit_key_file=secret_files["refresh_hmac_key_file"]))


def test_local_http_only_on_loopback(secret_files: dict[str, str]) -> None:
    validate_for_startup(Settings(env=Env.LOCAL, allowed_origins=["http://127.0.0.1:8080", "http://localhost:8080"], **secret_files))  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        validate_for_startup(Settings(env=Env.LOCAL, allowed_origins=["http://192.168.1.10:8080"], **secret_files))  # type: ignore[arg-type]
    assert Settings(env=Env.LOCAL, **secret_files).refresh_cookie_name == "pulseops_refresh_dev"  # type: ignore[arg-type]


def test_config_errors_do_not_contain_secret_values(secret_files: dict[str, str], tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text('{"current": 9, "keys": {"1": "CANARY-SECRET-abcdefabcdefabcdefabcdefabcdefabcdef"}}')
    with pytest.raises(ConfigError) as ei:
        validate_for_startup(hosted(secret_files, ingest_hmac_keys_file=str(tmp_path / "bad.json")))
    assert "CANARY" not in str(ei.value)


def _capture(fn) -> str:  # type: ignore[no-untyped-def]
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(AllowlistJsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    old = root.level
    root.setLevel(logging.DEBUG)
    logging.getLogger("pulseops").setLevel(logging.DEBUG)
    try:
        fn()
    finally:
        root.removeHandler(handler)
        root.setLevel(old)
    return buf.getvalue()


def test_log_allowlist_drops_unknown_fields_and_third_party_messages() -> None:
    def emit() -> None:
        log_event("test.event", status=200, email="CANARY-alice@example.com", body="CANARY-body", headers="CANARY-h")
        logging.getLogger("sqlalchemy.engine").warning("INSERT ... params CANARY-SECRET-bind")
        try:
            raise ValueError("CANARY-in-exception-message")
        except ValueError:
            logging.getLogger("uvicorn.error").exception("boom CANARY-x")

    out = _capture(emit)
    assert "CANARY" not in out
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[0]["event"] == "test.event" and lines[0]["status"] == 200
    assert lines[2]["error_class"] == "ValueError"


@pytest.mark.parametrize(
    ("pw", "expect"),
    [
        ("short-pass", ["too_short"]),
        ("x" * 129, ["too_long", "too_common"]),
        ("passwordpassword", ["too_common"]),
        ("PasswordPassword", ["too_common"]),
        ("aaaaaaaaaaaaaaaaaaaa", ["too_common"]),
        ("correct horse battery staple", ["too_common"]),
        ("violet-anchor-prism-42", []),
        ("ｖｉｏｌｅｔ－ａｎｃｈｏｒ－ｐｒｉｓｍ", []),  # fullwidth: normalized via NFKC
        ("p@ss wörd with spaces ok", []),
    ],
)
def test_password_policy(pw: str, expect: list[str]) -> None:
    assert policy_violations(pw) == expect


def test_password_matches_email_rejected() -> None:
    assert "matches_email" in policy_violations("someone@example.org", email="someone@example.org")


def test_hasher_argon2id_and_normalization() -> None:
    h = PasswordHasher(19456, 2, 1, 2)

    async def run() -> None:
        encoded = await h.hash("violet-anchor-prism-42")
        assert encoded.startswith("$argon2id$") and "m=19456,t=2,p=1" in encoded
        assert await h.verify("violet-anchor-prism-42", encoded)
        assert not await h.verify("violet-anchor-prism-43", encoded)
        assert not await h.verify("violet-anchor-prism-42", None)  # unknown user: dummy hash, always false
        fw = await h.hash("ｖｉｏｌｅｔ－ａｎｃｈｏｒ－ｐｒｉｓｍ")
        assert await h.verify("violet-anchor-prism", fw)
        assert await h.hash("same-password-twice!") != await h.hash("same-password-twice!")  # unique salts

    asyncio.run(run())
