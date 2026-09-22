"""Two bearer forms: the app-signed identity claim, verified offline, and the
bare Google access token, validated with one async userinfo call."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auth import external_oauth_provider as provider_module
from auth.external_oauth_provider import ExternalOAuthProvider
from auth.identity_claim import ClaimRefused, IdentityClaim, parse_public_keys, verify_identity_claim


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _keypair():
    private = Ed25519PrivateKey.generate()
    spki = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, base64.b64encode(spki).decode("ascii")


def _mint(private: Ed25519PrivateKey, **overrides) -> str:
    payload = {
        "typ": "google-mcp",
        "tok": "ya29.inner",
        "sub": "1234567890",
        "email": "user@example.test",
        "exp": int(time.time()) + 600,
    }
    payload.update(overrides)
    encoded = _b64url(json.dumps(payload).encode("utf-8"))
    return f"{encoded}.{_b64url(private.sign(encoded.encode('ascii')))}"


def _provider(*key_b64: str) -> ExternalOAuthProvider:
    return ExternalOAuthProvider(
        client_id="test-client",
        client_secret="test-client-secret",
        base_url="https://workspace-mcp.example.test",
        resource_server_url="https://workspace-mcp.example.test",
        required_scopes=["openid"],
        identity_claim_keys=parse_public_keys(",".join(key_b64)) if key_b64 else None,
    )


# --- the claim itself -------------------------------------------------------


def test_parse_public_keys_rejects_junk_and_empty():
    with pytest.raises(ValueError):
        parse_public_keys("")
    with pytest.raises(ValueError):
        parse_public_keys(base64.b64encode(b"not a key").decode())


def test_claim_verifies_against_any_configured_key():
    old, old_b64 = _keypair()
    new, new_b64 = _keypair()
    keys = parse_public_keys(f"{old_b64}, {new_b64}")
    for private in (old, new):
        claim = verify_identity_claim(_mint(private), keys)
        assert isinstance(claim, IdentityClaim)
        assert (claim.token, claim.sub, claim.email) == ("ya29.inner", "1234567890", "user@example.test")


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda t, p: _mint(p, typ="data"), "wrong-type"),
        (lambda t, p: _mint(p, exp=int(time.time()) - 1), "expired"),
        (lambda t, p: _mint(p, tok=None), "malformed"),
        (lambda t, p: t[:-4] + "AAAA", "bad-signature"),
        (lambda t, p: _mint(Ed25519PrivateKey.generate()), "bad-signature"),
        (lambda t, p: "no-dot-at-all", "malformed"),
    ],
)
def test_claim_refusals(mutate, reason):
    private, key_b64 = _keypair()
    keys = parse_public_keys(key_b64)
    refused = verify_identity_claim(mutate(_mint(private), private), keys)
    assert isinstance(refused, ClaimRefused)
    assert refused.reason == reason


# --- the provider -----------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_claim_yields_the_google_token_and_identity_without_network(monkeypatch):
    private, key_b64 = _keypair()
    provider = _provider(key_b64)

    def no_network():
        raise AssertionError("an identity claim must not reach the network")

    monkeypatch.setattr(provider_module, "shared_client", no_network)
    exp = int(time.time()) + 900
    access = await provider.verify_token(_mint(private, exp=exp))
    assert access is not None
    assert access.token == "ya29.inner"
    assert access.claims == {"email": "user@example.test", "sub": "1234567890"}
    assert access.email == "user@example.test"
    assert access.sub == "1234567890"
    assert access.expires_at == exp
    assert access.scopes == ["openid"]


@pytest.mark.asyncio
async def test_identity_claim_is_refused_with_no_keys_configured():
    private, _ = _keypair()
    assert await _provider().verify_token(_mint(private)) is None


@pytest.mark.asyncio
async def test_wrong_type_claim_is_refused_even_with_a_valid_signature():
    private, key_b64 = _keypair()
    assert await _provider(key_b64).verify_token(_mint(private, typ="data")) is None


@pytest.mark.asyncio
async def test_bare_google_token_takes_the_userinfo_path(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"id": "999", "email": "bare@example.test"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(provider_module, "shared_client", lambda: client)
    _, key_b64 = _keypair()
    access = await _provider(key_b64).verify_token("ya29.bare")
    assert seen == {"url": provider_module.GOOGLE_USERINFO_URL, "auth": "Bearer ya29.bare"}
    assert access is not None
    assert access.token == "ya29.bare"
    assert access.claims == {"email": "bare@example.test", "sub": "999"}


@pytest.mark.asyncio
async def test_bare_google_token_google_rejects_is_refused(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "invalid_token"})))
    monkeypatch.setattr(provider_module, "shared_client", lambda: client)
    assert await _provider().verify_token("ya29.dead") is None
