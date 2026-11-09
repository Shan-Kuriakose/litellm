"""Read-side aggregation over ``LiteLLM_DailyEndUserSpend``.

The daily rollup is populated every ~10–15 s by the proxy's background
writer (``litellm/proxy/db/daily_spend_bulk_upsert.py``), so these
functions only read from it — they never trigger a fresh aggregation.

All three functions return fully-shaped Pydantic responses; callers in
``dashboard_metrics.py`` are thin HTTP wrappers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from litellm._logging import verbose_proxy_logger

from .schemas import (
    Coverage,
    HealthResponse,
    MetricWithDelta,
    ModelSlice,
    PricingGap,
    PricingGapsResponse,
    TokenBreakdown,
    TrendPoint,
    UserMetricsRow,
    UsersMetricsResponse,
    Window,
)

_FRESHNESS_STALE_SEC = 300  # 5 min; the writer runs every 10–15 s


def _row_total_tokens(row: Any) -> int:
    return int(
        (row.prompt_tokens or 0)
        + (row.completion_tokens or 0)
        + (row.cache_read_input_tokens or 0)
        + (row.cache_creation_input_tokens or 0)
    )


def _delta_pct(current: float, previous: float) -> Optional[float]:
    if previous == 0:
        return None
    return ((current - previous) / previous) * 100.0


def _previous_window(start: str, end: str) -> tuple[str, str]:
    start_dt = date.fromisoformat(start)
    end_dt = date.fromisoformat(end)
    length_days = (end_dt - start_dt).days + 1
    prev_end = start_dt - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length_days - 1)
    return prev_start.isoformat(), prev_end.isoformat()


async def _fetch_rows(prisma_client: Any, start: str, end: str) -> list[Any]:
    return await prisma_client.db.litellm_dailyenduserspend.find_many(
        where={"date": {"gte": start, "lte": end}},
        order=[{"date": "desc"}, {"id": "asc"}],
    )


def _summarize(rows: list[Any]) -> dict[str, float]:
    cost = sum(float(r.spend or 0) for r in rows)
    tokens = sum(_row_total_tokens(r) for r in rows)
    requests = sum(int(r.api_requests or 0) for r in rows)
    successful = sum(int(r.successful_requests or 0) for r in rows)
    failed = sum(int(r.failed_requests or 0) for r in rows)
    latency_total = sum(int(r.total_response_time_ms or 0) for r in rows)
    timed = sum(int(r.timed_requests or 0) for r in rows)
    active_users = len({r.end_user_id for r in rows if r.end_user_id and (r.api_requests or 0) > 0})
    avg_latency_ms = (latency_total / timed) if timed > 0 else 0.0
    return {
        "cost": cost,
        "tokens": tokens,
        "requests": requests,
        "successful": successful,
        "failed": failed,
        "active_users": active_users,
        "avg_latency_ms": avg_latency_ms,
    }


def _build_kpis(current: dict[str, float], previous: dict[str, float]) -> dict[str, MetricWithDelta]:
    return {
        "cost": MetricWithDelta(
            value=current["cost"],
            deltaPct=_delta_pct(current["cost"], previous["cost"]),
        ),
        "activeUsers": MetricWithDelta(
            value=current["active_users"],
            deltaPct=_delta_pct(current["active_users"], previous["active_users"]),
        ),
        "requests": MetricWithDelta(
            value=current["requests"],
            deltaPct=_delta_pct(current["requests"], previous["requests"]),
        ),
        "tokens": MetricWithDelta(
            value=current["tokens"],
            deltaPct=_delta_pct(current["tokens"], previous["tokens"]),
        ),
        "avgLatencyMs": MetricWithDelta(
            value=current["avg_latency_ms"],
            deltaPct=_delta_pct(current["avg_latency_ms"], previous["avg_latency_ms"]),
        ),
    }


def _build_trend(rows: list[Any]) -> list[TrendPoint]:
    by_date: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = r.date
        bucket = by_date.setdefault(
            d, {"cost": 0.0, "tokens": 0, "requests": 0, "users": set()}
        )
        bucket["cost"] += float(r.spend or 0)
        bucket["tokens"] += _row_total_tokens(r)
        bucket["requests"] += int(r.api_requests or 0)
        if r.end_user_id and (r.api_requests or 0) > 0:
            bucket["users"].add(r.end_user_id)

    return sorted(
        (
            TrendPoint(
                date=d,
                cost=b["cost"],
                tokens=b["tokens"],
                requests=b["requests"],
                activeUsers=len(b["users"]),
            )
            for d, b in by_date.items()
        ),
        key=lambda p: p.date,
    )


def _build_model_mix(rows: list[Any]) -> list[ModelSlice]:
    by_model: dict[str, dict[str, Any]] = {}
    for r in rows:
        m = r.model or "unknown"
        bucket = by_model.setdefault(m, {"cost": 0.0, "tokens": 0, "requests": 0})
        bucket["cost"] += float(r.spend or 0)
        bucket["tokens"] += _row_total_tokens(r)
        bucket["requests"] += int(r.api_requests or 0)

    return sorted(
        (
            ModelSlice(model=m, cost=b["cost"], tokens=b["tokens"], requests=b["requests"])
            for m, b in by_model.items()
        ),
        key=lambda s: -s.cost,
    )


def _build_token_breakdown(rows: list[Any]) -> list[TokenBreakdown]:
    by_date: dict[str, dict[str, int]] = {}
    for r in rows:
        d = r.date
        bucket = by_date.setdefault(
            d, {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        )
        bucket["input"] += int(r.prompt_tokens or 0)
        bucket["output"] += int(r.completion_tokens or 0)
        bucket["cacheRead"] += int(r.cache_read_input_tokens or 0)
        bucket["cacheWrite"] += int(r.cache_creation_input_tokens or 0)

    return sorted(
        (TokenBreakdown(date=d, **b) for d, b in by_date.items()),
        key=lambda t: t.date,
    )


def _build_users(rows: list[Any]) -> list[UserMetricsRow]:
    by_email: dict[str, dict[str, Any]] = {}
    for r in rows:
        email = r.end_user_id
        if not email:
            continue
        bucket = by_email.setdefault(
            email,
            {
                "cost": 0.0,
                "tokens": 0,
                "requests": 0,
                "successful": 0,
                "failed": 0,
                "latency_total": 0,
                "timed": 0,
                "models": set(),
                "last_active": r.date,
            },
        )
        bucket["cost"] += float(r.spend or 0)
        bucket["tokens"] += _row_total_tokens(r)
        bucket["requests"] += int(r.api_requests or 0)
        bucket["successful"] += int(r.successful_requests or 0)
        bucket["failed"] += int(r.failed_requests or 0)
        bucket["latency_total"] += int(r.total_response_time_ms or 0)
        bucket["timed"] += int(r.timed_requests or 0)
        if r.model:
            bucket["models"].add(r.model)
        if r.date and r.date > bucket["last_active"]:
            bucket["last_active"] = r.date

    def _cost_source(b: dict[str, Any]) -> str:
        if b["requests"] == 0:
            return "unknown"
        return "litellm" if b["cost"] > 0 else "unknown"

    return sorted(
        (
            UserMetricsRow(
                email=email,
                cost=b["cost"],
                tokens=b["tokens"],
                requests=b["requests"],
                successfulRequests=b["successful"],
                failedRequests=b["failed"],
                avgLatencyMs=(b["latency_total"] / b["timed"]) if b["timed"] > 0 else None,
                models=sorted(b["models"]),
                lastActive=b["last_active"],
                costSource=_cost_source(b),
            )
            for email, b in by_email.items()
        ),
        key=lambda u: -u.cost,
    )


def _build_coverage(rows: list[Any]) -> Coverage:
    # A model is a "pricing gap" iff it has at least one successful request
    # across the window AND total spend across all its rows is 0.
    stats: dict[str, dict[str, Any]] = {}
    for r in rows:
        m = r.model or "unknown"
        s = stats.setdefault(m, {"spend": 0.0, "requests": 0, "successful": 0})
        s["spend"] += float(r.spend or 0)
        s["requests"] += int(r.api_requests or 0)
        s["successful"] += int(r.successful_requests or 0)

    with_pricing: list[str] = []
    without_pricing: list[str] = []
    for m, s in stats.items():
        if s["successful"] == 0:
            continue
        if s["spend"] > 0:
            with_pricing.append(m)
        else:
            without_pricing.append(m)

    requests_unknown = sum(s["requests"] for m, s in stats.items() if m in without_pricing)
    requests_total = sum(s["requests"] for s in stats.values())
    pct_unknown = (requests_unknown / requests_total * 100.0) if requests_total > 0 else 0.0

    return Coverage(
        modelsWithPricing=sorted(with_pricing),
        modelsWithoutPricing=sorted(without_pricing),
        requestsWithUnknownCost=requests_unknown,
        requestsWithUnknownCostPct=pct_unknown,
    )


async def get_user_metrics(
    prisma_client: Any,
    start: str,
    end: str,
    granularity: str,
) -> UsersMetricsResponse:
    current_rows = await _fetch_rows(prisma_client, start, end)
    prev_start, prev_end = _previous_window(start, end)
    previous_rows = await _fetch_rows(prisma_client, prev_start, prev_end)

    current = _summarize(current_rows)
    previous = _summarize(previous_rows)

    return UsersMetricsResponse(
        window=Window.model_validate({"from": start, "to": end, "granularity": granularity}),
        kpis=_build_kpis(current, previous),
        trend=_build_trend(current_rows),
        modelMix=_build_model_mix(current_rows),
        tokenBreakdown=_build_token_breakdown(current_rows),
        users=_build_users(current_rows),
        coverage=_build_coverage(current_rows),
    )


async def get_pricing_gaps(
    prisma_client: Any,
    start: str,
    end: str,
) -> PricingGapsResponse:
    rows = await _fetch_rows(prisma_client, start, end)
    by_key: dict[tuple[str, Optional[str]], dict[str, Any]] = {}
    for r in rows:
        key = (r.model or "unknown", r.custom_llm_provider)
        bucket = by_key.setdefault(
            key,
            {"spend": 0.0, "requests": 0, "successful": 0, "tokens": 0},
        )
        bucket["spend"] += float(r.spend or 0)
        bucket["requests"] += int(r.api_requests or 0)
        bucket["successful"] += int(r.successful_requests or 0)
        bucket["tokens"] += _row_total_tokens(r)

    gaps = [
        PricingGap(
            model=model,
            provider=provider,
            observedRequests=b["requests"],
            observedTokens=b["tokens"],
        )
        for (model, provider), b in by_key.items()
        if b["spend"] == 0.0 and b["successful"] > 0
    ]
    gaps.sort(key=lambda g: -g.observedRequests)

    return PricingGapsResponse(
        window=Window.model_validate({"from": start, "to": end, "granularity": "day"}),
        gaps=gaps,
    )


async def get_health(prisma_client: Any) -> HealthResponse:
    now = datetime.now(timezone.utc)
    try:
        recent = await prisma_client.db.litellm_dailyenduserspend.find_many(
            order=[{"updated_at": "desc"}],
            take=1,
        )
    except Exception as e:
        verbose_proxy_logger.exception("dashboard-metrics health probe failed: %s", e)
        return HealthResponse(status="no-data", rollupFreshSec=None, checkedAt=now.isoformat())

    if not recent:
        return HealthResponse(status="no-data", rollupFreshSec=None, checkedAt=now.isoformat())

    last_updated = recent[0].updated_at
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=timezone.utc)

    fresh_sec = int((now - last_updated).total_seconds())
    status_label = "ok" if fresh_sec <= _FRESHNESS_STALE_SEC else "stale"
    return HealthResponse(status=status_label, rollupFreshSec=fresh_sec, checkedAt=now.isoformat())
