"""Shared-bearer-token auth for /dashboard-metrics/*.

Deliberately separate from ``user_api_key_auth`` (which validates virtual
keys against the DB) and from the master-key check: the dashboard is an
external caller with no LiteLLM-user identity, and rotating its token
should not require touching the virtual-key or master-key systems.

Configuration: set ``DASHBOARD_METRICS_TOKEN`` in the proxy environment.
If the env var is unset the dependency fails closed (401), so a
misconfigured proxy cannot accidentally expose the metrics routes.
"""

from __future__ import annotations

import hmac
import os
from typing import Final, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from litellm._logging import verbose_proxy_logger

DASHBOARD_METRICS_TOKEN_ENV_VAR: Final = "DASHBOARD_METRICS_TOKEN"

_bearer_scheme: Final = HTTPBearer(auto_error=False)

_UNAUTH_HEADERS: Final = {"WWW-Authenticate": "Bearer"}


async def dashboard_bearer_token_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> None:
    expected = os.getenv(DASHBOARD_METRICS_TOKEN_ENV_VAR)
    if not expected:
        verbose_proxy_logger.warning(
            "%s is not set — /dashboard-metrics/* routes are rejecting all requests.",
            DASHBOARD_METRICS_TOKEN_ENV_VAR,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "dashboard metrics endpoint not configured"},
            headers=_UNAUTH_HEADERS,
        )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing or invalid Authorization header"},
            headers=_UNAUTH_HEADERS,
        )

    if not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid token"},
            headers=_UNAUTH_HEADERS,
        )
