"""
The app-signed identity claim.

Fieldwork (the app in front of this server) holds the user's Google access
token and, from the id_token Google returned at consent, the account's subject
and email. Rather than have this server re-derive that identity by calling
Google's userinfo endpoint on every request, the app signs the three facts
together and sends the result as the bearer:

    base64url(json payload) "." base64url(Ed25519 signature over the payload bytes)

The envelope, the key format and the type check mirror the app's datalake
claims (`src/lib/datalake/claim.ts`, verified by the s3 broker), so one signing
key serves several audiences and `typ` keeps them apart: this server accepts
only `typ == "google-mcp"`, the broker only `typ == "data"`.

Payload:
    typ    "google-mcp"
    tok    the Google access token, used verbatim for every Google API call
    sub    Google account subject
    email  Google account email
    exp    epoch seconds; the Google token's own expiry
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from typing import Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key

CLAIM_TYPE = "google-mcp"


@dataclass(frozen=True)
class IdentityClaim:
    token: str
    sub: str
    email: str
    exp: int


@dataclass(frozen=True)
class ClaimRefused:
    reason: str  # malformed | bad-signature | wrong-type | expired


def parse_public_keys(raw: str) -> list[Ed25519PublicKey]:
    """`DATA_CLAIM_PUBLIC_KEYS`: comma-separated base64 DER (SubjectPublicKeyInfo)
    Ed25519 keys, the same value the s3 broker reads. Raises on anything that is
    not such a key: a misconfigured verifier must fail at startup, not accept
    nothing at runtime."""
    keys: list[Ed25519PublicKey] = []
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        key = load_der_public_key(base64.b64decode(entry, validate=True))
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("DATA_CLAIM_PUBLIC_KEYS entry is not an Ed25519 key")
        keys.append(key)
    if not keys:
        raise ValueError("DATA_CLAIM_PUBLIC_KEYS holds no keys")
    return keys


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def looks_like_identity_claim(token: str) -> bool:
    """Two base64url segments. A Google access token has no dot; a JWT has two."""
    return token.count(".") == 1


def verify_identity_claim(
    token: str, keys: list[Ed25519PublicKey], now: float | None = None
) -> Union[IdentityClaim, ClaimRefused]:
    dot = token.rfind(".")
    if dot <= 0:
        return ClaimRefused("malformed")
    encoded = token[:dot]
    try:
        signature = _b64url_decode(token[dot + 1 :])
    except (binascii.Error, ValueError):
        return ClaimRefused("malformed")
    data = encoded.encode("ascii", errors="strict") if encoded.isascii() else None
    if data is None:
        return ClaimRefused("malformed")

    for key in keys:
        try:
            key.verify(signature, data)
            break
        except InvalidSignature:
            continue
    else:
        return ClaimRefused("bad-signature")

    try:
        payload = json.loads(_b64url_decode(encoded))
    except (binascii.Error, ValueError):
        return ClaimRefused("malformed")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("tok"), str)
        or not isinstance(payload.get("sub"), str)
        or not isinstance(payload.get("email"), str)
        or not isinstance(payload.get("exp"), (int, float))
        or isinstance(payload.get("exp"), bool)
    ):
        return ClaimRefused("malformed")
    if payload.get("typ") != CLAIM_TYPE:
        return ClaimRefused("wrong-type")
    if (now if now is not None else time.time()) > payload["exp"]:
        return ClaimRefused("expired")
    return IdentityClaim(
        token=payload["tok"],
        sub=payload["sub"],
        email=payload["email"],
        exp=int(payload["exp"]),
    )
