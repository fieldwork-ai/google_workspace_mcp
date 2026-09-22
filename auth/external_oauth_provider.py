"""
External OAuth Provider for Google Workspace MCP

This server is a resource server only: it validates the bearer each request
carries and never issues tokens. Two bearer forms are accepted:

- An app-signed identity claim (`auth.identity_claim`): the app in front of
  this server already knows the Google account's subject and email from the
  consent it ran, so it signs them together with the Google access token. The
  signature is verified offline against `DATA_CLAIM_PUBLIC_KEYS`, no network.
- A bare Google access token (`ya29.*`), validated with one async call to
  Google's userinfo endpoint. Kept while callers still send this form.

Standard JWT ID tokens fall through to FastMCP's GoogleProvider as before.
"""

import functools
import logging
import os
import time
from typing import Optional

from starlette.routing import Route
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.auth import AccessToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from auth.identity_claim import (
    IdentityClaim,
    looks_like_identity_claim,
    verify_identity_claim,
)
from auth.oauth_types import WorkspaceAccessToken
from core.async_bridge import shared_client

logger = logging.getLogger(__name__)

# Google's OAuth 2.0 Authorization Server
GOOGLE_ISSUER_URL = "https://accounts.google.com"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# Configurable session time in seconds (default: 1 hour, max: 24 hours)
_DEFAULT_SESSION_TIME = 3600
_MAX_SESSION_TIME = 86400


@functools.lru_cache(maxsize=1)
def get_session_time() -> int:
    """Parse SESSION_TIME from environment with fallback, min/max clamp.

    Result is cached; changes require a server restart.
    """
    raw = os.getenv("SESSION_TIME", "")
    if not raw:
        return _DEFAULT_SESSION_TIME
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid SESSION_TIME=%r, falling back to %d", raw, _DEFAULT_SESSION_TIME
        )
        return _DEFAULT_SESSION_TIME
    clamped = max(1, min(value, _MAX_SESSION_TIME))
    if clamped != value:
        logger.warning(
            "SESSION_TIME=%d clamped to %d (allowed range: 1–%d)",
            value,
            clamped,
            _MAX_SESSION_TIME,
        )
    return clamped


class ExternalOAuthProvider(GoogleProvider):
    """
    Extended GoogleProvider that supports validating external Google OAuth access tokens.

    This provider handles ya29.* access tokens by calling Google's userinfo API,
    while maintaining compatibility with standard JWT ID tokens.

    Unlike the standard GoogleProvider, this acts as a Resource Server only:
    - Does NOT create /authorize, /token, /register endpoints
    - Only advertises Google's authorization server in metadata
    - Only validates tokens, does not issue them
    """

    def __init__(
        self,
        client_id: str,
        client_secret: Optional[str] = None,
        resource_server_url: Optional[str] = None,
        identity_claim_keys: Optional[list[Ed25519PublicKey]] = None,
        **kwargs,
    ):
        """Initialize and store client credentials for token validation.

        `identity_claim_keys` are the Ed25519 public keys app-signed identity
        claims are verified against; with none, only bare Google access tokens
        and JWTs are accepted.
        """
        self._resource_server_url = resource_server_url
        if resource_server_url and "resource_base_url" not in kwargs:
            kwargs["resource_base_url"] = resource_server_url
        super().__init__(client_id=client_id, client_secret=client_secret, **kwargs)
        # Store credentials as they're not exposed by parent class
        self._client_id = client_id
        self._client_secret = client_secret
        # Store as string - Pydantic validates it when passed to models
        self.resource_server_url = self._resource_server_url
        self._identity_claim_keys = list(identity_claim_keys or [])

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """
        Verify a bearer: an app-signed identity claim, a bare Google access
        token (ya29.*), or a JWT ID token (delegated to the parent class).

        Returns:
            AccessToken object if valid, None otherwise
        """
        if token.startswith("ya29."):
            user_info = await self._fetch_userinfo(token)
            if not user_info or not user_info.get("email"):
                logger.warning("bearer form=google-access-token refused: userinfo did not identify the account")
                return None
            logger.info("bearer form=google-access-token accepted for: %s", user_info["email"])
            return self._access_token(token, email=user_info["email"], sub=user_info.get("id"))

        if looks_like_identity_claim(token):
            if not self._identity_claim_keys:
                logger.warning("bearer form=identity-claim refused: no DATA_CLAIM_PUBLIC_KEYS configured")
                return None
            claim = verify_identity_claim(token, self._identity_claim_keys)
            if not isinstance(claim, IdentityClaim):
                logger.warning("bearer form=identity-claim refused: %s", claim.reason)
                return None
            logger.info("bearer form=identity-claim accepted for: %s", claim.email)
            return self._access_token(claim.token, email=claim.email, sub=claim.sub, expires_at=claim.exp)

        # For JWT tokens, use parent class implementation
        return await super().verify_token(token)

    def _access_token(
        self, google_token: str, *, email: str, sub: Optional[str], expires_at: Optional[int] = None
    ) -> WorkspaceAccessToken:
        """The one access-token shape every form produces; `token` is the
        Google access token the handlers call Google with."""
        return WorkspaceAccessToken(
            token=google_token,
            scopes=list(getattr(self, "required_scopes", []) or []),
            expires_at=expires_at if expires_at is not None else int(time.time()) + get_session_time(),
            claims={"email": email, "sub": sub},
            client_id=self._client_id,
            email=email,
            sub=sub,
        )

    async def _fetch_userinfo(self, token: str) -> Optional[dict]:
        """Ask Google whose token this is: one GET on the shared client."""
        try:
            response = await shared_client().get(
                GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {token}"}
            )
        except Exception as exc:  # noqa: BLE001 - a refusal, logged, never a crash
            logger.error("Error validating external access token: %s", exc)
            return None
        if response.status_code != 200:
            logger.error("userinfo answered %s for an external access token", response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            logger.error("userinfo answered with a body that is not JSON")
            return None

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """
        Get OAuth routes for external provider mode.

        Returns only protected resource metadata routes that point to Google
        as the authorization server. Does not create authorization server routes
        (/authorize, /token, etc.) since tokens are issued by Google directly.

        Args:
            mcp_path: Path where FastMCP mounts the protected MCP endpoint.

        Returns:
            List of routes - only protected resource metadata
        """
        from mcp.server.auth.routes import create_protected_resource_routes

        if not self.resource_server_url:
            logger.warning(
                "ExternalOAuthProvider: resource_server_url not set, no routes created"
            )
            return []

        self.set_mcp_path(mcp_path)
        resource_url = self._get_resource_url(mcp_path)
        if not resource_url:
            logger.warning(
                "ExternalOAuthProvider: protected resource URL could not be resolved"
            )
            return []

        # Create protected resource routes that point to Google as the authorization server
        # Pass strings directly - Pydantic validates them during model construction
        protected_routes = create_protected_resource_routes(
            resource_url=resource_url,
            authorization_servers=[GOOGLE_ISSUER_URL],
            scopes_supported=self.required_scopes,
            resource_name="Google Workspace MCP",
            resource_documentation=None,
        )

        logger.info(
            f"ExternalOAuthProvider: Created protected resource routes pointing to {GOOGLE_ISSUER_URL}"
        )
        return protected_routes
