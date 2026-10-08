"""Supabase-shaped tokens, signed with a keypair this suite generates."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

TOKEN_ISSUER = "https://project.supabase.co/auth/v1"
TOKEN_KID = "test-signing-key"
TOKEN_SUBJECT = "8f14e45f-ceea-467a-9c1e-3f2a1b6c7d80"


def token_keypair(algorithm: str):
    if algorithm == "ES256":
        private = ec.generate_private_key(ec.SECP256R1())
    else:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return private, private_pem


class StubJWKSClient:
    """Stands in for PyJWKClient, returning the public half of our keypair."""

    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return type("Key", (), {"key": self._public_key})()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
