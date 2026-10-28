"""Token validation. Transport metadata is not authorization: every call is
checked for issuer, audience (this surface's resource URL), expiry and scope."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

import jwt

from . import config


class AuthError(Exception):
    def __init__(self, status: int, error: str, description: str, scope: Optional[str] = None):
        super().__init__(description)
        self.status, self.error, self.description, self.scope = status, error, description, scope

    def www_authenticate(self, surface: str) -> str:
        meta = f"{config.PUBLIC_BASE_URL}/.well-known/oauth-protected-resource/mcp/{surface}"
        parts = [f'error="{self.error}"', f'error_description="{self.description}"', f'resource_metadata="{meta}"']
        if self.scope:
            parts.append(f'scope="{self.scope}"')
        return "Bearer " + ", ".join(parts)


@dataclass
class Caller:
    sub: str
    issuer: str
    audience: str
    scopes: List[str] = field(default_factory=list)
    level: int = 0

    @property
    def name(self) -> str:
        return config.USERS.get(self.sub, {}).get("name", self.sub)


def mint(sub: str, audience: str, ttl_s: int = 3600, issuer: str = config.TRUSTED_ISSUER,
         scopes: Optional[List[str]] = None, secret: str = config.TOKEN_SECRET, level: Optional[int] = None) -> str:
    """Stand-in for the authorization server, used by the demo and tests."""
    user = config.USERS.get(sub, {})
    now = int(time.time())
    claims = {
        "iss": issuer, "sub": sub, "aud": audience, "iat": now, "exp": now + ttl_s,
        "scope": " ".join(scopes if scopes is not None else user.get("scopes", [])),
        "level": level if level is not None else user.get("level", 0),
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def authenticate(authorization: Optional[str], surface: str) -> Caller:
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError(401, "invalid_request", "Bearer token required")
    token = authorization[len("Bearer "):].strip()
    audience = config.resource_url(surface)
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise AuthError(401, "invalid_token", "Malformed token")
    # Check the issuer before trusting any key: credentials are keyed by issuer.
    if unverified.get("iss") != config.TRUSTED_ISSUER:
        raise AuthError(401, "invalid_token", "Untrusted issuer")
    try:
        claims = jwt.decode(token, config.TOKEN_SECRET, algorithms=["HS256"], audience=audience,
                            issuer=config.TRUSTED_ISSUER, options={"require": ["exp", "iss", "aud", "sub"]})
    except jwt.ExpiredSignatureError:
        raise AuthError(401, "invalid_token", "Token expired")
    except jwt.InvalidAudienceError:
        raise AuthError(401, "invalid_token", f"Token audience is not {audience}")
    except jwt.PyJWTError:
        raise AuthError(401, "invalid_token", "Invalid token")
    return Caller(sub=claims["sub"], issuer=claims["iss"], audience=audience,
                  scopes=str(claims.get("scope", "")).split(), level=int(claims.get("level", 0)))


def require_scope(caller: Caller, scope: str) -> None:
    if scope not in caller.scopes:
        raise AuthError(403, "insufficient_scope", f"Requires scope {scope}", scope=scope)
