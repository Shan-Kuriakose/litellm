"""Bearer-token auth for /dashboard-metrics/*.

Hashes the incoming bearer with SHA-256 and looks it up in the
``LiteLLM_DashboardToken`` table. Returns a ``DashboardScope`` describing
whether the caller holds a global or team-scoped token.

Deliberately separate from ``user_api_key_auth`` (which validates virtual
keys) and from the master-key check: dashboard consumers are external
callers with no LiteLLM-user identity, and rotating their tokens should
not require touching the virtual-key or master-key systems.

Two distinct 401 bodies:
  * header-shape failures (missing / non-Bearer / empty credential)
  * token-resolution failures (hash not present OR hash present but
    ``revoked_at IS NOT NULL``) → **identical** response for both
    sub-cases, so an attacker cannot confirm whether a specific token
    hash exists in the table.
"""

from __future__ import annotations

import hashlib
from typing import Final, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from litellm._logging import verbose_proxy_logger
from litellm.proxy.auth.dashboard_scope import DashboardScope

# Retained for backward compatibility with any external imports (e.g. tests or
# ops scripts that referenced the env var). The bootstrap hook in
# ``dashboard_metrics_endpoints/bootstrap.py`` is the only remaining reader
# of the env var itself; request-time auth no longer consults it.
DASHBOARD_METRICS_TOKEN_ENV_VAR: Final = "DASHBOARD_METRICS_TOKEN"

_bearer_scheme: Final = HTTPBearer(auto_error=False)

_UNAUTH_HEADERS: Final = {"WWW-Authenticate": "Bearer"}


async def dashboard_bearer_token_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> DashboardScope:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not credentials.credentials
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing bearer token"},
            headers=_UNAUTH_HEADERS,
        )

    token_hash = hashlib.sha256(credentials.credentials.encode()).hexdigest()

    # Lazy import matches the pattern used throughout the proxy
    # (e.g. customer_endpoints.py, dashboard_metrics.py) so this module
    # can be imported before the global ``prisma_client`` is initialized.
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        verbose_proxy_logger.error(
            "dashboard_bearer_token_auth called before prisma_client is initialized"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "database not connected"},
        )

    # SECURITY — load-bearing single query.
    # The ``revoked_at: None`` filter is what collapses "unknown hash" and
    # "revoked hash" into a single code path with an identical 401 response.
    # Do NOT split this into two queries (e.g. "fetch row, then check
    # revoked") without replacing the information-hiding invariant some
    # other way — a two-query shape would let timing distinguish the cases,
    # and any added log line could leak existence of a revoked hash.
    # See test_401_revoked_and_unknown_return_byte_identical_401 for the
    # regression contract.
    row = await prisma_client.db.litellm_dashboardtoken.find_first(
        where={"token_hash": token_hash, "revoked_at": None}
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid dashboard token"},
            headers=_UNAUTH_HEADERS,
        )

    return DashboardScope(
        scope_type=row.scope_type,
        team_id=row.team_id,
        label=row.label,
    )
