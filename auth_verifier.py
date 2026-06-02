"""OAuth Resource-Server token validation for the FlexReport MCP server.

The MCP server is an OAuth 2.0 Resource Server: it validates the inbound bearer
token (issued by the backend Authorization Server) and lets the MCP SDK serve the
protected-resource metadata + 401 challenges. It never sees a password.

Validation uses the backend's published JWKS (RS256 public key), so this service
holds no signing secret — it stays a credential-free proxy.

Config (env), aligned to the backend's `OAuthProvider`:
  OAUTH_ISSUER      token `iss` + the advertised authorization server
                    (backend default: https://app.flexreportfinapi.com)
  OAUTH_AUDIENCE    expected token `aud`. The backend falls back to the issuer when
                    its own OAUTH_AUDIENCE is unset, so we mirror that default.
                    Set both sides to the canonical MCP URL for true audience binding.
  OAUTH_JWKS_URL    where to fetch the public keys (default: {issuer}/.well-known/jwks.json)
  MCP_RESOURCE_URL  this server's canonical resource identifier (the PRM `resource`)
"""

import os

import anyio
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

OAUTH_ISSUER = os.environ.get("OAUTH_ISSUER", "https://app.flexreportfinapi.com")
OAUTH_AUDIENCE = os.environ.get("OAUTH_AUDIENCE", OAUTH_ISSUER)
OAUTH_JWKS_URL = os.environ.get(
    "OAUTH_JWKS_URL", f"{OAUTH_ISSUER.rstrip('/')}/.well-known/jwks.json"
)
MCP_RESOURCE_URL = os.environ.get(
    "MCP_RESOURCE_URL", "https://mcp.flexreportfinapi.com/mcp"
)

# PyJWKClient caches fetched keys; create it lazily so import never does network I/O.
_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(OAUTH_JWKS_URL)
    return _jwks_client


def _decode_rs256(token: str) -> dict:
    """Validate signature (via JWKS), `aud`, `iss`, `exp`. Raises on any failure."""
    signing_key = _jwks().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=OAUTH_AUDIENCE,
        issuer=OAUTH_ISSUER,
    )


class FlexReportTokenVerifier(TokenVerifier):
    """Validate the backend's RS256 OAuth access tokens.

    `lenient` (AUTH_MODE=both): tokens that fail RS256 validation are passed through
    as opaque — the backend re-validates and 401s anything invalid. This keeps legacy
    static-JWT (HS256) header users working during the transition without this service
    ever holding the legacy secret. AUTH_MODE=oauth sets lenient=False (strict).
    """

    def __init__(self, lenient: bool):
        self.lenient = lenient

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # JWKS lookup + decode are sync (network on cache-miss) — keep off the loop.
            claims = await anyio.to_thread.run_sync(_decode_rs256, token)
        except Exception:
            if self.lenient:
                return AccessToken(
                    token=token,
                    client_id="legacy",
                    scopes=[],
                    expires_at=None,
                    resource=MCP_RESOURCE_URL,
                )
            return None
        return AccessToken(
            token=token,
            client_id=claims.get("client_id", ""),
            scopes=(claims.get("scope") or "").split(),
            expires_at=claims.get("exp"),
            resource=MCP_RESOURCE_URL,
        )
