"""Resolved scope attached to a /dashboard-metrics/* request by the bearer-token auth.

A ``DashboardScope`` is produced by ``dashboard_bearer_token_auth`` after it hashes
the incoming bearer and finds a matching live row in ``LiteLLM_DashboardToken``.
Endpoints receive it via ``Depends`` and use it to decide whether to apply a
team filter (``scope_type == 'team'``) or return all rows (``scope_type == 'global'``).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class DashboardScope(BaseModel):
    scope_type: Literal["global", "team"]
    team_id: Optional[str]
    label: Optional[str]
