"""Pydantic response models for /dashboard-metrics/*.

The response shape is frozen in claude-dashboard/LITELLM_INTEGRATION.md §6.2.
Email is the identity field exposed to the dashboard — LiteLLM's
``end_user_id`` is renamed to ``email`` on the way out so the dashboard
can key on exactly the same identifier it already uses for Claude Code
(``user.email`` from OTel).
"""

from __future__ import annotations

from typing import Final, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class MetricWithDelta(BaseModel):
    value: float
    deltaPct: Optional[float] = None


class TrendPoint(BaseModel):
    date: str
    cost: float
    tokens: int
    requests: int
    activeUsers: int


class ModelSlice(BaseModel):
    model: str
    cost: float
    tokens: int
    requests: int


class TokenBreakdown(BaseModel):
    date: str
    input: int
    output: int
    cacheRead: int
    cacheWrite: int


CostSource = Literal["litellm", "estimated", "unknown"]


class UserMetricsRow(BaseModel):
    email: str
    cost: float
    tokens: int
    requests: int
    successfulRequests: int
    failedRequests: int
    avgLatencyMs: Optional[float]
    models: list[str]
    lastActive: Optional[str]
    costSource: CostSource


class Coverage(BaseModel):
    modelsWithPricing: list[str]
    modelsWithoutPricing: list[str]
    requestsWithUnknownCost: int
    requestsWithUnknownCostPct: float


class Window(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    start: str = Field(alias="from")
    end: str = Field(alias="to")
    granularity: str


class UsersMetricsResponse(BaseModel):
    window: Window
    kpis: dict[str, MetricWithDelta]
    trend: list[TrendPoint]
    modelMix: list[ModelSlice]
    tokenBreakdown: list[TokenBreakdown]
    users: list[UserMetricsRow]
    coverage: Coverage


class PricingGap(BaseModel):
    model: str
    provider: Optional[str]
    observedRequests: int
    observedTokens: int


class PricingGapsResponse(BaseModel):
    window: Window
    gaps: list[PricingGap]


HealthStatus = Literal["ok", "stale", "no-data"]


class HealthResponse(BaseModel):
    status: HealthStatus
    rollupFreshSec: Optional[int]
    checkedAt: str


KPI_KEYS: Final = ("cost", "activeUsers", "requests", "tokens", "avgLatencyMs")
