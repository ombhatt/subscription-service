"""Token verification, against a keypair this suite generates.

Deliberately not against a real Supabase project: the suite would then depend on
a third party being reachable, and could not mint the malformed tokens that are
the whole point. Every case below is something an attacker can send.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException

from app import auth
from tests.conftest import TOKEN_ISSUER, TOKEN_KID, TOKEN_SUBJECT, token_keypair


@pytest.fixture(params=["RS256", "ES256"])
def token_algorithm(request) -> str:
    """Supabase signs with whichever the project was created with. New projects
    default to ES256; the docs describe RS256 as the default, so both are pinned
    in ALLOWED_ALGORITHMS and both are exercised here. Testing only RSA would
    have left the algorithm actually in use uncovered."""
    return request.param


def test_a_valid_token_identifies_the_user(signer):
    claims = auth.verify_token(signer())
    assert claims["sub"] == TOKEN_SUBJECT
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


def test_a_token_signed_by_a_different_key_is_refused(signer, token_algorithm):
    _, other_pem = token_keypair(token_algorithm)
    forged = jwt.encode(
        {
            "sub": "attacker",
            "aud": "authenticated",
            "iss": TOKEN_ISSUER,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        other_pem,
        algorithm=token_algorithm,
        headers={"kid": TOKEN_KID},
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
            "iss": TOKEN_ISSUER,
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
    assert user.id == TOKEN_SUBJECT
