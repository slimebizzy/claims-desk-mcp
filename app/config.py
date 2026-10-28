"""Settings, identities and the authority table.

Everything a replica needs is read from the environment or from this module, so
two replicas started with the same environment are interchangeable.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent

PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = [PROTOCOL_VERSION]

SERVER_VERSION = "1.0.0"

# The public base URL is part of every token audience and approval binding,
# so it must be the address clients use, not the replica's own port.
PUBLIC_BASE_URL = os.environ.get("CLAIMS_DESK_PUBLIC_URL", "http://127.0.0.1:8800").rstrip("/")
DB_PATH = Path(os.environ.get("CLAIMS_DESK_DB", ROOT / "data" / "claims.db"))
REPLICA = os.environ.get("CLAIMS_DESK_REPLICA", "r1")
DEV_MODE = os.environ.get("CLAIMS_DESK_DEV", "1") == "1"

TRUSTED_ISSUER = "https://auth.cobaltharbor.example"
# Local demo only: a shared HS256 secret stands in for the authorization
# server's signing key. Production validates RS256 tokens against its JWKS.
TOKEN_SECRET = os.environ.get("CLAIMS_DESK_TOKEN_SECRET", "dev-only-secret-change-me-0123456789abcdef")
APPROVAL_SECRET = os.environ.get("CLAIMS_DESK_APPROVAL_SECRET", "dev-only-approval-key-0123456789abcdef")

ALLOWED_ORIGINS = {PUBLIC_BASE_URL, "http://localhost:8800", "http://127.0.0.1:8800"}

LIST_TTL_MS = 300_000
APPROVAL_TTL_S = 15 * 60
REQUEST_TTL_S = 24 * 60 * 60

# Two surfaces with separate audiences: a token for "read" cannot call "claims".
SURFACES: Dict[str, dict] = {
    "read": {
        "registry_name": "io.github.slimebizzy/claims-desk-read",
        "title": "Claims Desk (read-only)",
        "scopes": ["claims:read"],
        "tools": ["claim.get", "policy.search"],
    },
    "claims": {
        "registry_name": "io.github.slimebizzy/claims-desk-reserves",
        "title": "Claims Desk (reserves)",
        "scopes": ["claims:read", "claims:write"],
        "tools": ["approval.status", "claim.get", "claim.set_reserve"],
    },
}


def resource_url(surface: str) -> str:
    return f"{PUBLIC_BASE_URL}/mcp/{surface}"


# Claims Handling Guidelines §2.1. None means no ceiling.
AUTHORITY = {1: 25_000, 2: 100_000, 3: 250_000, 4: None}
LEVEL_TITLES = {1: "Associate Adjuster", 2: "Senior Adjuster", 3: "Claims Supervisor", 4: "Regional Claims Manager"}
CAT_LEVEL1_LIMIT = 50_000  # §2.3, windstorm and hail during a declared catastrophe


def authority_limit(level: int) -> Optional[int]:
    return AUTHORITY.get(level, 0)


def level_needed(amount: int) -> int:
    for level in (1, 2, 3):
        if amount <= AUTHORITY[level]:
            return level
    return 4


# Fictional staff. The dev token endpoint mints tokens only for these.
USERS: Dict[str, dict] = {
    "dana.ruiz": {"name": "Dana Ruiz", "level": 1, "scopes": ["claims:read", "claims:write"]},
    "sam.okafor": {"name": "Sam Okafor", "level": 2, "scopes": ["claims:read", "claims:write"]},
    "priya.nair": {"name": "Priya Nair", "level": 3, "scopes": ["claims:read", "claims:approve"]},
    "lee.chen": {"name": "Lee Chen", "level": 4, "scopes": ["claims:read", "claims:approve"]},
    "copilot-svc": {"name": "Coverage Copilot service", "level": 0, "scopes": ["claims:read"]},
}
