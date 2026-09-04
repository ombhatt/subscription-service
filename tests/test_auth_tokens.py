"""Token verification, against a keypair this suite generates.

Deliberately not against a real Supabase project: the suite would then depend on
a third party being reachable, and could not mint the malformed tokens that are
the whole point. Every case below is something an attacker can send.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app import auth

ISSUER = "https://project.supabase.co/auth/v1"
KID = "test-signing-key"


def _keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return private, private_pem


class _StubJWKSClient:
    """Stands in for PyJWKClient, returning the public half of our keypair."""

    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return type("Key", (), {"key": self._public_key})()


@pytest.fixture
def signer(monkeypatch):
    """A working Supabase-shaped setup: configured URL and a known signing key."""
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")

    from app.config import get_settings

    get_settings.cache_clear()
    private, private_pem = _keypair()
    auth.set_jwks_client(_StubJWKSClient(private.public_key()))

    def mint(**overrides) -> str:
        now = datetime.now(UTC)
        claims = {
            "sub": "8f14e45f-ceea-467a-9c1e-3f2a1b6c7d80",
            "email": "someone@example.com",
            "aud": "authenticated",
            "iss": ISSUER,
            "role": "authenticated",
            "iat": now,
            "exp": now + timedelta(hours=1),
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": KID})

    yield mint

    auth.set_jwks_client(None)
    get_settings.cache_clear()


def test_a_valid_token_identifies_the_user(signer):
    claims = auth.verify_token(signer())
    assert claims["sub"] == "8f14e45f-ceea-467a-9c1e-3f2a1b6c7d80"
    assert claims["email"] == "someone@example.com"


def test_an_expired_token_is_refused(signer):
    expired = signer(exp=datetime.now(UTC) - timedelta(minutes=1))
    with pytest.raises(HTTPException) as raised:
        auth.verify_token(expired)
    assert raised.value.status_code == 401
    assert raised.value.detail == "session expired"


def test_a_token_for_another_audience_is_refused(signer):
    """An anon-key token is signed by the same project; only `aud` separates it
    from a signed-in user's."""
    with pytest.raises(HTTPException) as raised:
        auth.verify_token(signer(aud="anon"))
    assert raised.value.status_code == 401


def test_a_token_from_another_issuer_is_refused(signer):
    """Someone else's Supabase project signs perfectly valid JWTs."""
    with pytest.raises(HTTPException) as raised:
        auth.verify_token(signer(iss="https://attacker.supabase.co/auth/v1"))
    assert raised.value.status_code == 401


def test_a_token_without_a_subject_is_refused(signer):
    """Without `sub` there is no user id, and every downstream lookup would run
    against None."""
    with pytest.raises(HTTPException) as raised:
        auth.verify_token(signer(sub=None))
    assert raised.value.status_code == 401


def test_a_token_signed_by_a_different_key_is_refused(signer):
    _, other_pem = _keypair()
    forged = jwt.encode(
        {
            "sub": "attacker",
            "aud": "authenticated",
            "iss": ISSUER,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        other_pem,
        algorithm="RS256",
        headers={"kid": KID},
    )
    with pytest.raises(HTTPException) as raised:
        auth.verify_token(forged)
    assert raised.value.status_code == 401


def test_an_unsigned_token_is_refused(signer):
    """`alg: none` is the oldest JWT attack there is."""
    unsigned = jwt.encode(
        {
            "sub": "attacker",
            "aud": "authenticated",
            "iss": ISSUER,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(HTTPException) as raised:
        auth.verify_token(unsigned)
    assert raised.value.status_code == 401


def test_the_error_does_not_say_which_check_failed(signer):
    """Which validation failed is useful to an attacker and useless to a client."""
    for bad in (signer(aud="anon"), signer(iss="https://elsewhere/auth/v1")):
        with pytest.raises(HTTPException) as raised:
            auth.verify_token(bad)
        assert raised.value.detail == "invalid session"


@pytest.mark.parametrize(
    "header",
    [None, "", "token abc", "Bearer", "Basic abc", "bearer"],
)
async def test_a_malformed_authorization_header_is_refused(header):
    with pytest.raises(HTTPException) as raised:
        await auth.get_current_user(authorization=header)
    assert raised.value.status_code == 401
    assert raised.value.headers["WWW-Authenticate"] == "Bearer"


async def test_a_lowercase_bearer_scheme_is_accepted(signer):
    """Schemes are case-insensitive per RFC 7235, and real clients vary."""
    user = await auth.get_current_user(authorization=f"bearer {signer()}")
    assert user.id == "8f14e45f-ceea-467a-9c1e-3f2a1b6c7d80"
