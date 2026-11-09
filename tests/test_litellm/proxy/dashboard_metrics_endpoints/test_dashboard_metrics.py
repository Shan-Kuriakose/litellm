"""Tests for the /dashboard-metrics/* routes and their query helpers.

Scope:
  * Auth dependency — 401 without/with-wrong-token, fail-closed when env unset,
    200 with the right token.
  * Response shape — contract match for ``/users``.
  * Delta semantics — present when previous window has data, ``None`` when empty.
  * Coverage math — ``requestsWithUnknownCost`` increments for ``spend == 0``.
  * ``pricing-gaps`` — distinct models with spend=0 and successful > 0.
  * ``/health`` — ``rollupFreshSec`` derived from ``MAX(updated_at)``.

No real database; we mock the Prisma client and install a fake
``litellm.proxy.proxy_server`` module so the handler's lazy import resolves
to our mock instead of loading the full 20 k-line proxy server.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from litellm.proxy.auth.dashboard_bearer_token_auth import (
    DASHBOARD_METRICS_TOKEN_ENV_VAR,
)
from litellm.proxy.dashboard_metrics_endpoints import dashboard_metrics_router

TEST_TOKEN = "test-dashboard-token-0123456789abcdef"


def _row(**overrides: Any) -> SimpleNamespace:
    """Build a minimal ``LiteLLM_DailyEndUserSpend`` row."""
    now = datetime.now(timezone.utc)
    defaults: dict[str, Any] = dict(
        id=f"rid-{overrides.get('date','x')}-{overrides.get('model','m')}-{overrides.get('end_user_id','u')}",
        end_user_id="user@example.com",
        date="2026-11-05",
        api_key="sk-test",
        model="gpt-4o",
        model_group="gpt-4o",
        custom_llm_provider="openai",
        mcp_namespaced_tool_name=None,
        endpoint="/v1/chat/completions",
        prompt_tokens=1000,
        completion_tokens=500,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        compression_saved_tokens=0,
        compression_savings_spend=0.0,
        prompt_caching_savings_spend=0.0,
        gateway_injected_caching_savings_spend=0.0,
        autorouter_savings_spend=0.0,
        spend=0.05,
        api_requests=1,
        successful_requests=1,
        failed_requests=0,
        total_response_time_ms=500,
        timed_requests=1,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_find_many(by_range: dict[tuple[str, str], list[SimpleNamespace]]):
    """Return an async ``find_many`` that routes on the ``where.date`` range."""

    async def find_many(
        where: dict[str, Any] | None = None,
        order: list[dict[str, str]] | None = None,
        take: int | None = None,
        **_: Any,
    ) -> list[SimpleNamespace]:
        if where and "date" in where:
            key = (where["date"]["gte"], where["date"]["lte"])
            return list(by_range.get(key, []))
        # Health probe: no ``where``, order by updated_at desc, take=1.
        all_rows: list[SimpleNamespace] = [r for v in by_range.values() for r in v]
        all_rows.sort(key=lambda r: r.updated_at, reverse=True)
        return all_rows[: (take or len(all_rows))]

    return find_many


def _mock_prisma(find_many) -> MagicMock:
    prisma = MagicMock()
    table = MagicMock()
    table.find_many = find_many
    prisma.db.litellm_dailyenduserspend = table
    return prisma


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """Build a test app with a hookable mock Prisma client.

    Yields ``(TestClient, set_prisma)`` where ``set_prisma(mock)`` wires a new
    prisma client for the next request. The fake ``litellm.proxy.proxy_server``
    module stays installed for the lifetime of the test.
    """
    fake_proxy_server = MagicMock()
    fake_proxy_server.prisma_client = None
    monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", fake_proxy_server)

    app = FastAPI()
    app.include_router(dashboard_metrics_router)
    tc = TestClient(app)

    def set_prisma(mock: MagicMock | None) -> None:
        fake_proxy_server.prisma_client = mock

    yield tc, set_prisma


# ─── Auth ─────────────────────────────────────────────────────────────────


def test_401_without_token(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    tc, _ = client
    r = tc.get("/dashboard-metrics/users?from=2026-11-01&to=2026-11-05")
    assert r.status_code == 401


def test_401_with_wrong_token(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    tc, _ = client
    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


def test_401_when_env_var_unset(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, raising=False)
    tc, _ = client
    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 401


# ─── Shape / Deltas / Coverage ────────────────────────────────────────────


def test_users_returns_200_and_correct_shape(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    rows = [
        _row(date="2026-11-01", spend=1.0, api_requests=10, successful_requests=10),
        _row(
            date="2026-11-02",
            spend=2.0,
            api_requests=20,
            successful_requests=20,
            end_user_id="alice@example.com",
        ),
    ]
    find_many = _make_find_many(
        {
            ("2026-11-01", "2026-11-05"): rows,
            ("2026-10-27", "2026-10-31"): [],
        }
    )
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many))

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["window"] == {"from": "2026-11-01", "to": "2026-11-05", "granularity": "day"}
    assert set(body["kpis"].keys()) == {"cost", "activeUsers", "requests", "tokens", "avgLatencyMs"}
    assert body["kpis"]["cost"]["value"] == pytest.approx(3.0)
    assert body["kpis"]["requests"]["value"] == pytest.approx(30.0)
    assert body["kpis"]["activeUsers"]["value"] == pytest.approx(2.0)

    assert len(body["users"]) == 2
    emails = {u["email"] for u in body["users"]}
    assert emails == {"user@example.com", "alice@example.com"}

    assert "trend" in body and len(body["trend"]) == 2
    assert "modelMix" in body and body["modelMix"][0]["model"] == "gpt-4o"
    assert "tokenBreakdown" in body
    assert "coverage" in body


def test_delta_is_null_when_previous_window_empty(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    find_many = _make_find_many(
        {
            ("2026-11-01", "2026-11-05"): [
                _row(date="2026-11-01", spend=1.0, api_requests=10, successful_requests=10)
            ],
            ("2026-10-27", "2026-10-31"): [],
        }
    )
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many))

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 200
    kpis = r.json()["kpis"]
    assert kpis["cost"]["deltaPct"] is None
    assert kpis["requests"]["deltaPct"] is None


def test_delta_computed_when_previous_window_has_data(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    find_many = _make_find_many(
        {
            ("2026-11-01", "2026-11-05"): [
                _row(date="2026-11-01", spend=10.0, api_requests=100, successful_requests=100)
            ],
            ("2026-10-27", "2026-10-31"): [
                _row(date="2026-10-27", spend=5.0, api_requests=50, successful_requests=50)
            ],
        }
    )
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many))

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 200
    kpis = r.json()["kpis"]
    assert kpis["cost"]["deltaPct"] == pytest.approx(100.0)
    assert kpis["requests"]["deltaPct"] == pytest.approx(100.0)


def test_coverage_counts_zero_cost_requests(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    rows = [
        _row(
            date="2026-11-01",
            model="priced-model",
            spend=1.0,
            api_requests=100,
            successful_requests=100,
        ),
        _row(
            date="2026-11-02",
            model="unpriced-model",
            spend=0.0,
            api_requests=40,
            successful_requests=40,
        ),
    ]
    find_many = _make_find_many(
        {
            ("2026-11-01", "2026-11-05"): rows,
            ("2026-10-27", "2026-10-31"): [],
        }
    )
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many))

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 200
    cov = r.json()["coverage"]
    assert "priced-model" in cov["modelsWithPricing"]
    assert "unpriced-model" in cov["modelsWithoutPricing"]
    assert cov["requestsWithUnknownCost"] == 40
    assert cov["requestsWithUnknownCostPct"] == pytest.approx(100 * 40 / 140)


# ─── Pricing-gaps route ───────────────────────────────────────────────────


def test_pricing_gaps_lists_only_distinct_zero_cost_models(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    rows = [
        _row(date="2026-11-01", model="a", spend=1.0, successful_requests=10),
        _row(
            date="2026-11-02",
            model="b",
            spend=0.0,
            successful_requests=5,
            custom_llm_provider="openai",
        ),
        _row(
            date="2026-11-03",
            model="c",
            spend=0.0,
            successful_requests=3,
            custom_llm_provider="custom",
        ),
    ]
    find_many = _make_find_many({("2026-11-01", "2026-11-05"): rows})
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many))

    r = tc.get(
        "/dashboard-metrics/pricing-gaps?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 200
    gaps = r.json()["gaps"]
    assert sorted(g["model"] for g in gaps) == ["b", "c"]


# ─── Health route ─────────────────────────────────────────────────────────


def test_health_returns_rollup_freshness(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    recent = datetime.now(timezone.utc) - timedelta(seconds=10)
    find_many = _make_find_many({("sentinel", "sentinel"): [_row(updated_at=recent)]})
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many))

    r = tc.get(
        "/dashboard-metrics/health",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert 0 <= body["rollupFreshSec"] < 60


def test_health_reports_stale_when_rollup_old(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    find_many = _make_find_many({("sentinel", "sentinel"): [_row(updated_at=old)]})
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many))

    r = tc.get(
        "/dashboard-metrics/health",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "stale"
    assert body["rollupFreshSec"] >= 3600 - 5


def test_health_reports_no_data_when_table_empty(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    find_many = _make_find_many({})
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many))

    r = tc.get(
        "/dashboard-metrics/health",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no-data"
    assert body["rollupFreshSec"] is None


# ─── Input validation ────────────────────────────────────────────────────


def test_users_rejects_malformed_date(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    tc, set_prisma = client
    set_prisma(_mock_prisma(_make_find_many({})))

    r = tc.get(
        "/dashboard-metrics/users?from=not-a-date&to=2026-11-05",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 400


def test_users_rejects_reversed_window(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, TEST_TOKEN)
    tc, set_prisma = client
    set_prisma(_mock_prisma(_make_find_many({})))

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-05&to=2026-11-01",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 400
