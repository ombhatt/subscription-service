"""Authentication.

Sessions come from Supabase Auth as JWTs signed with the project's asymmetric
key. We verify them locally against the project's JWKS, so no call to Supabase
sits in the hot path of every request, and a rotated or revoked key takes effect
without deploying anything.

There is deliberately no development bypass here. An "accept this header and
trust it" path is the kind of thing that survives into production; the test
suite overrides the dependency instead, which cannot.

One rule that outlives any provider: resolve the tier from *this* service, never
from a claim in the token. Tokens are minted before a downgrade and stay valid
after it.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient

from app.config import get_settings

DEFAULT_ADMIN_KEY = "change-me-in-prod"

# Algorithms Supabase signs with. Pinned so a forged token cannot select `none`
# or downgrade to a symmetric algorithm and have its signature checked against
# a public key.
ALLOWED_ALGORITHMS = ["RS256", "ES256"]


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str | None = None


_jwks_client: PyJWKClient | None = None


def get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        settings = get_settings()
        if not settings.supabase_url:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SUPABASE_URL is not configured",
            )
        _jwks_client = PyJWKClient(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
            lifespan=settings.jwks_cache_seconds,
        )
    return _jwks_client


def set_jwks_client(client: PyJWKClient | None) -> None:
    """Test seam, and the way to reset after a configuration change."""
    global _jwks_client
    _jwks_client = client


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authorization header must be 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def verify_token(token: str) -> dict:
    """Decode and validate a Supabase access token, or raise 401.

    Signature, expiry, audience and issuer are all checked. `sub` is required
    because it is the only thing we use to identify the user, and a token
    without one would otherwise sail through and produce a `None` user id.
    """
    settings = get_settings()
    issuer = f"{settings.supabase_url.rstrip('/')}/auth/v1"

    try:
        signing_key = get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=ALLOWED_ALGORITHMS,
            audience=settings.supabase_jwt_audience,
            issuer=issuer,
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.PyJWTError as exc:
        # Deliberately vague to the caller: which check failed is useful to an
        # attacker and useless to a legitimate client. The detail goes to logs.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid session",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    claims = verify_token(_bearer_token(authorization))
    return CurrentUser(id=claims["sub"], email=claims.get("email"))


async def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """Gate on the admin key, in constant time.

    These endpoints grant tiers and extend grace windows, so the comparison is
    `compare_digest` rather than `==`: a plain comparison returns as soon as two
    bytes differ, which leaks the key one character at a time to anyone willing
    to measure. Nothing here rate-limits attempts, so that leak is worth closing
    even though it is fiddly to exploit over a network.
    """
    settings = get_settings()
    expected = settings.admin_api_key

    # An unset or still-default key means these endpoints are effectively open.
    # Refuse rather than authenticate against a value published in .env.example.
    if not expected or (settings.is_production and expected == DEFAULT_ADMIN_KEY):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_API_KEY is unset or still the default",
        )

    if not x_admin_key or not secrets.compare_digest(
        x_admin_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin key required")
