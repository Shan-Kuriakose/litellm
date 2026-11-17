"""Startup bootstrap for the dashboard-token table.

Seeds the ``LiteLLM_DashboardToken`` global row from the environment variable
``DASHBOARD_METRICS_TOKEN``. Runs once on proxy boot, idempotent, fail-open.

Behavior:
  * If ``DASHBOARD_METRICS_TOKEN`` is unset → no-op, log at info.
  * Compute ``sha256(raw_token).hexdigest()``.
  * If a live global row with the same hash already exists → no-op (idempotent).
  * If a live global row with a different hash exists → no-op, log at warning.
    (Rotating the global token is an operator action, not a bootstrap action.)
  * Otherwise insert a new global row with ``created_by='bootstrap'``.

The hook catches and logs any exception — it must not block proxy startup if
the DB is unreachable or the schema is unexpected.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Final

from litellm._logging import verbose_proxy_logger

DASHBOARD_METRICS_TOKEN_ENV_VAR: Final = "DASHBOARD_METRICS_TOKEN"


async def seed_global_dashboard_token(prisma_client: Any) -> None:
    """Idempotent, fail-open bootstrap of the global dashboard-token row."""
    try:
        raw_token = os.getenv(DASHBOARD_METRICS_TOKEN_ENV_VAR)
        if not raw_token:
            verbose_proxy_logger.info(
                "%s unset — skipping global dashboard token bootstrap.",
                DASHBOARD_METRICS_TOKEN_ENV_VAR,
            )
            return

        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        # Already seeded with the same hash → done.
        existing = await prisma_client.db.litellm_dashboardtoken.find_first(
            where={
                "token_hash": token_hash,
                "scope_type": "global",
                "revoked_at": None,
            }
        )
        if existing is not None:
            return

        # A live global row with a different hash is an operator-initiated
        # state; don't silently overwrite it.
        any_live_global = await prisma_client.db.litellm_dashboardtoken.find_first(
            where={"scope_type": "global", "revoked_at": None}
        )
        if any_live_global is not None:
            verbose_proxy_logger.warning(
                "A live global dashboard token already exists under a different "
                "hash; leaving it in place. To rotate, revoke the existing row "
                "manually (see claude-dashboard/docs/per-team-dashboard/09-open-items.md)."
            )
            return

        await prisma_client.db.litellm_dashboardtoken.create(
            data={
                "token_hash": token_hash,
                "team_id": None,
                "scope_type": "global",
                "label": "Global",
                "created_by": "bootstrap",
            }
        )
        verbose_proxy_logger.info(
            "Seeded global dashboard token row (scope_type='global')."
        )
    except Exception as exc:  # fail-open: never block proxy startup
        verbose_proxy_logger.warning(
            "Global dashboard token bootstrap failed (continuing startup): %s", exc
        )
