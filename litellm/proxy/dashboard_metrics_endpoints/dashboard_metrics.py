"""HTTP routes for /dashboard-metrics/*.

See ``LITELLM_INTEGRATION.md §6`` in the claude-dashboard repo for the
contract. Three routes, all gated on the same shared bearer token:

  GET /dashboard-metrics/users?from=YYYY-MM-DD&to=YYYY-MM-DD&granularity=day
  GET /dashboard-metrics/pricing-gaps?from=YYYY-MM-DD&to=YYYY-MM-DD
  GET /dashboard-metrics/health
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, status

from litellm._logging import verbose_proxy_logger
from litellm.proxy.auth.dashboard_bearer_token_auth import dashboard_bearer_token_auth

from .queries import get_health, get_pricing_gaps, get_user_metrics
from .schemas import HealthResponse, PricingGapsResponse, UsersMetricsResponse

router: Final = APIRouter(
    prefix="/dashboard-metrics",
    tags=["dashboard-metrics"],
    dependencies=[Depends(dashboard_bearer_token_auth)],
)

_ALLOWED_GRANULARITIES: Final = frozenset({"day", "week", "month"})


def _validate_date(value: str, name: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": f"invalid {name!r}: expected YYYY-MM-DD"},
        ) from exc
    return value


def _validate_window(start: str, end: str) -> tuple[str, str]:
    start = _validate_date(start, "from")
    end = _validate_date(end, "to")
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "'from' must be on or before 'to'"},
        )
    return start, end


def _get_prisma_client() -> object:
    # Imported lazily so this module can be imported before the proxy's
    # global prisma_client is initialized (matches the pattern used in
    # customer_endpoints.py:164 and elsewhere).
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "database not connected"},
        )
    return prisma_client


@router.get("/users", response_model=UsersMetricsResponse, response_model_by_alias=True)
async def users_metrics_route(
    start: Annotated[str, Query(alias="from")],
    end: Annotated[str, Query(alias="to")],
    granularity: str = "day",
) -> UsersMetricsResponse:
    start, end = _validate_window(start, end)
    if granularity not in _ALLOWED_GRANULARITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "granularity must be one of: day, week, month"},
        )

    prisma_client = _get_prisma_client()
    try:
        return await get_user_metrics(prisma_client, start, end, granularity)
    except HTTPException:
        raise
    except Exception as exc:
        verbose_proxy_logger.exception("dashboard-metrics /users failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": f"failed to fetch metrics: {exc}"},
        ) from exc


@router.get("/pricing-gaps", response_model=PricingGapsResponse, response_model_by_alias=True)
async def pricing_gaps_route(
    start: Annotated[str, Query(alias="from")],
    end: Annotated[str, Query(alias="to")],
) -> PricingGapsResponse:
    start, end = _validate_window(start, end)

    prisma_client = _get_prisma_client()
    try:
        return await get_pricing_gaps(prisma_client, start, end)
    except HTTPException:
        raise
    except Exception as exc:
        verbose_proxy_logger.exception("dashboard-metrics /pricing-gaps failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": f"failed to fetch pricing gaps: {exc}"},
        ) from exc


@router.get("/health", response_model=HealthResponse)
async def health_route() -> HealthResponse:
    prisma_client = _get_prisma_client()
    try:
        return await get_health(prisma_client)
    except HTTPException:
        raise
    except Exception as exc:
        verbose_proxy_logger.exception("dashboard-metrics /health failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": f"failed to check health: {exc}"},
        ) from exc
