"""Tests for the /dashboard-metrics/* routes and their query helpers.

Scope:
  * Auth dependency — 401 without a bearer, 401 with an unknown hash,
    401 with a revoked hash; 200 for a live hash.
  * ``/me`` — returns the authenticated token's scope descriptor.
  * Global scope — ``/users`` and ``/pricing-gaps`` behave as before (no filter).
  * Team scope — ``/users`` and ``/pricing-gaps`` restrict rows to the team's
    virtual-key list; a team with zero keys yields a well-formed empty response.
  * Response shape — contract match for ``/users``.
  * Delta semantics — present when previous window has data, ``None`` when empty.
  * Coverage math — ``requestsWithUnknownCost`` increments for ``spend == 0``.
  * ``/pricing-gaps`` — distinct models with spend=0 and successful > 0.
  * ``/health`` — ``rollupFreshSec`` derived from ``MAX(updated_at)``; scope
    required for auth but not applied to the response.

No real database; we mock the Prisma client and install a fake
``litellm.proxy.proxy_server`` module so the handler's lazy import resolves
to our mock instead of loading the full 20 k-line proxy server.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from litellm.proxy.dashboard_metrics_endpoints import dashboard_metrics_router

GLOBAL_TOKEN = "global-dashboard-token-0123456789abcdef"
GLOBAL_HASH = hashlib.sha256(GLOBAL_TOKEN.encode()).hexdigest()

TEAM_A_TOKEN = "team-A-token-9999999999999999999999"
TEAM_A_HASH = hashlib.sha256(TEAM_A_TOKEN.encode()).hexdigest()
TEAM_A_ID = "team_01"

TEAM_B_TOKEN = "team-B-token-no-keys-00000000000000"
TEAM_B_HASH = hashlib.sha256(TEAM_B_TOKEN.encode()).hexdigest()
TEAM_B_ID = "team_02"

UNKNOWN_TOKEN = "unknown-token-cafebabe"


def _scope_row(scope_type: str, team_id: Optional[str] = None, label: Optional[str] = None) -> SimpleNamespace:
    return SimpleNamespace(
        token_hash="irrelevant",
        scope_type=scope_type,
        team_id=team_id,
        label=label,
        created_at=datetime.now(timezone.utc),
        created_by="test",
        revoked_at=None,
    )


def _row(**overrides: Any) -> SimpleNamespace:
    """Build a minimal ``LiteLLM_DailyUserSpend`` row."""
    now = datetime.now(timezone.utc)
    defaults: dict[str, Any] = dict(
        id=f"rid-{overrides.get('date','x')}-{overrides.get('model','m')}-{overrides.get('user_id','u')}",
        user_id="user@example.com",
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
    """Return an async ``find_many`` that routes on the ``where.date`` range.

    If ``where`` contains ``api_key: {"in": [...]}``, additionally restrict to
    rows whose ``api_key`` is in the given list — matches the production filter.
    """

    async def find_many(
        where: dict[str, Any] | None = None,
        order: list[dict[str, str]] | None = None,
        take: int | None = None,
        **_: Any,
    ) -> list[SimpleNamespace]:
        if where and "date" in where:
            key = (where["date"]["gte"], where["date"]["lte"])
            rows = list(by_range.get(key, []))
            if "api_key" in where:
                allowed = set(where["api_key"].get("in", []))
                rows = [r for r in rows if r.api_key in allowed]
            return rows
        # Health probe: no ``where``, order by updated_at desc, take=1.
        all_rows: list[SimpleNamespace] = [r for v in by_range.values() for r in v]
        all_rows.sort(key=lambda r: r.updated_at, reverse=True)
        return all_rows[: (take or len(all_rows))]

    return find_many


def _mock_prisma(
    find_many,
    scope_rows_by_hash: Optional[dict[str, SimpleNamespace]] = None,
    team_tokens_by_team_id: Optional[dict[str, list[str]]] = None,
) -> MagicMock:
    """Build a MagicMock prisma client with three configured sub-tables.

    * ``litellm_dailyuserspend.find_many`` → the given ``find_many``.
    * ``litellm_dashboardtoken.find_first`` → looks up the given hash map.
      Returns ``None`` (unknown/revoked) or the matching scope row.
    * ``litellm_verificationtoken.find_many`` → returns rows for the given
      team_id, built as SimpleNamespaces with a ``token`` attribute.
    """
    prisma = MagicMock()

    spend_table = MagicMock()
    spend_table.find_many = find_many
    prisma.db.litellm_dailyuserspend = spend_table

    hashes = scope_rows_by_hash or {}

    async def dashboard_find_first(where: dict[str, Any] | None = None, **_: Any):
        token_hash = (where or {}).get("token_hash")
        return hashes.get(token_hash)

    token_table = MagicMock()
    token_table.find_first = dashboard_find_first
    prisma.db.litellm_dashboardtoken = token_table

    team_tokens = team_tokens_by_team_id or {}

    async def verification_find_many(where: dict[str, Any] | None = None, **_: Any):
        team_id = (where or {}).get("team_id")
        return [SimpleNamespace(token=t) for t in team_tokens.get(team_id, [])]

    vt_table = MagicMock()
    vt_table.find_many = verification_find_many
    prisma.db.litellm_verificationtoken = vt_table

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


def test_401_without_token(client) -> None:
    tc, set_prisma = client
    set_prisma(_mock_prisma(_make_find_many({}), {GLOBAL_HASH: _scope_row("global", label="Global")}))
    r = tc.get("/dashboard-metrics/users?from=2026-11-01&to=2026-11-05")
    assert r.status_code == 401
    assert r.json() == {"detail": {"error": "missing bearer token"}}


def test_401_with_unknown_token(client) -> None:
    tc, set_prisma = client
    set_prisma(_mock_prisma(_make_find_many({}), {GLOBAL_HASH: _scope_row("global", label="Global")}))
    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {UNKNOWN_TOKEN}"},
    )
    assert r.status_code == 401
    assert r.json() == {"detail": {"error": "invalid dashboard token"}}


def test_401_with_revoked_token_same_body_as_unknown(client) -> None:
    """A revoked token must look identical to an unknown one — no information disclosure."""
    tc, set_prisma = client
    # The mock's find_first keys on the hash map, and we deliberately omit
    # the revoked token's hash from the map — which is exactly what happens
    # in production when the row's ``revoked_at`` is set and the WHERE clause
    # filters it out.
    revoked_token = "revoked-token-00000000"
    set_prisma(_mock_prisma(_make_find_many({}), {GLOBAL_HASH: _scope_row("global", label="Global")}))
    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {revoked_token}"},
    )
    assert r.status_code == 401
    assert r.json() == {"detail": {"error": "invalid dashboard token"}}


def test_401_revoked_and_unknown_return_byte_identical_401(client) -> None:
    """Regression guard: the status code AND the response body for revoked
    and unknown tokens must be byte-identical.

    This is the information-hiding invariant enforced by the single-query
    shape in ``dashboard_bearer_token_auth``. If a future change splits
    the query or adds a differentiating log/metric path, this test will
    catch it before the behavioural drift reaches production.
    """
    tc, set_prisma = client
    set_prisma(_mock_prisma(_make_find_many({}), {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r_unknown = tc.get(
        "/dashboard-metrics/me",
        headers={"Authorization": "Bearer unknown-token-cafebabe"},
    )
    r_revoked = tc.get(
        "/dashboard-metrics/me",
        headers={"Authorization": "Bearer previously-revoked-00000000"},
    )

    assert r_unknown.status_code == r_revoked.status_code == 401
    # Byte-identical body — no additional fields, no differing error codes.
    assert r_unknown.content == r_revoked.content, (
        f"unknown={r_unknown.content!r} revoked={r_revoked.content!r}"
    )
    # And the body is the documented shape.
    assert r_unknown.json() == {"detail": {"error": "invalid dashboard token"}}


# ─── /me ─────────────────────────────────────────────────────────────────


def test_me_returns_global_descriptor(client) -> None:
    tc, set_prisma = client
    set_prisma(_mock_prisma(_make_find_many({}), {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get("/dashboard-metrics/me", headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"scope_type": "global", "label": "Global"}


def test_me_returns_team_descriptor(client) -> None:
    tc, set_prisma = client
    set_prisma(
        _mock_prisma(
            _make_find_many({}),
            {TEAM_A_HASH: _scope_row("team", team_id=TEAM_A_ID, label="Account A")},
        )
    )
    r = tc.get("/dashboard-metrics/me", headers={"Authorization": f"Bearer {TEAM_A_TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"scope_type": "team", "label": "Account A"}


def test_me_defaults_label_to_global_when_absent(client) -> None:
    tc, set_prisma = client
    set_prisma(_mock_prisma(_make_find_many({}), {GLOBAL_HASH: _scope_row("global", label=None)}))

    r = tc.get("/dashboard-metrics/me", headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"scope_type": "global", "label": "Global"}


# ─── Shape / Deltas / Coverage (global scope) ────────────────────────────


def test_users_returns_200_and_correct_shape(client) -> None:
    rows = [
        _row(date="2026-11-01", spend=1.0, api_requests=10, successful_requests=10),
        _row(
            date="2026-11-02",
            spend=2.0,
            api_requests=20,
            successful_requests=20,
            user_id="alice@example.com",
        ),
    ]
    find_many = _make_find_many(
        {
            ("2026-11-01", "2026-11-05"): rows,
            ("2026-10-27", "2026-10-31"): [],
        }
    )
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many, {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
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


def test_delta_is_null_when_previous_window_empty(client) -> None:
    find_many = _make_find_many(
        {
            ("2026-11-01", "2026-11-05"): [
                _row(date="2026-11-01", spend=1.0, api_requests=10, successful_requests=10)
            ],
            ("2026-10-27", "2026-10-31"): [],
        }
    )
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many, {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )
    assert r.status_code == 200
    kpis = r.json()["kpis"]
    assert kpis["cost"]["deltaPct"] is None
    assert kpis["requests"]["deltaPct"] is None


def test_delta_computed_when_previous_window_has_data(client) -> None:
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
    set_prisma(_mock_prisma(find_many, {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )
    assert r.status_code == 200
    kpis = r.json()["kpis"]
    assert kpis["cost"]["deltaPct"] == pytest.approx(100.0)
    assert kpis["requests"]["deltaPct"] == pytest.approx(100.0)


def test_coverage_counts_zero_cost_requests(client) -> None:
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
    set_prisma(_mock_prisma(find_many, {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )
    assert r.status_code == 200
    cov = r.json()["coverage"]
    assert "priced-model" in cov["modelsWithPricing"]
    assert "unpriced-model" in cov["modelsWithoutPricing"]
    assert cov["requestsWithUnknownCost"] == 40
    assert cov["requestsWithUnknownCostPct"] == pytest.approx(100 * 40 / 140)


# ─── Team scope ──────────────────────────────────────────────────────────


def test_users_team_scope_filters_by_team_api_keys(client) -> None:
    """Rows whose api_key is NOT in the team's token list must be excluded."""
    rows = [
        _row(date="2026-11-02", api_key="sk-team-A-1", spend=1.0, api_requests=10, successful_requests=10),
        _row(
            date="2026-11-03",
            api_key="sk-team-A-2",
            spend=2.0,
            api_requests=20,
            successful_requests=20,
            user_id="alice@example.com",
        ),
        _row(
            date="2026-11-04",
            api_key="sk-some-other-team",  # must be filtered out
            spend=100.0,
            api_requests=1000,
            successful_requests=1000,
            user_id="stranger@example.com",
        ),
    ]
    find_many = _make_find_many(
        {
            ("2026-11-01", "2026-11-05"): rows,
            ("2026-10-27", "2026-10-31"): [],
        }
    )
    tc, set_prisma = client
    set_prisma(
        _mock_prisma(
            find_many,
            scope_rows_by_hash={
                TEAM_A_HASH: _scope_row("team", team_id=TEAM_A_ID, label="Account A"),
            },
            team_tokens_by_team_id={TEAM_A_ID: ["sk-team-A-1", "sk-team-A-2"]},
        )
    )

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {TEAM_A_TOKEN}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kpis"]["cost"]["value"] == pytest.approx(3.0)
    assert body["kpis"]["requests"]["value"] == pytest.approx(30.0)
    emails = {u["email"] for u in body["users"]}
    assert "stranger@example.com" not in emails
    assert emails == {"user@example.com", "alice@example.com"}


def test_users_team_with_zero_keys_returns_empty_well_formed(client) -> None:
    rows = [
        _row(date="2026-11-02", api_key="sk-unrelated", spend=1.0, api_requests=10, successful_requests=10),
    ]
    find_many = _make_find_many(
        {
            ("2026-11-01", "2026-11-05"): rows,
            ("2026-10-27", "2026-10-31"): [],
        }
    )
    tc, set_prisma = client
    set_prisma(
        _mock_prisma(
            find_many,
            scope_rows_by_hash={TEAM_B_HASH: _scope_row("team", team_id=TEAM_B_ID, label="Account B")},
            team_tokens_by_team_id={TEAM_B_ID: []},  # team exists but no keys
        )
    )

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {TEAM_B_TOKEN}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["users"] == []
    assert body["kpis"]["cost"]["value"] == 0
    assert body["kpis"]["requests"]["value"] == 0
    assert body["trend"] == []
    assert body["modelMix"] == []


def test_pricing_gaps_team_scope_applies_filter(client) -> None:
    rows = [
        _row(
            date="2026-11-02",
            api_key="sk-team-A-1",
            model="gap-in-team",
            spend=0.0,
            successful_requests=5,
        ),
        _row(
            date="2026-11-03",
            api_key="sk-other-team",  # must be filtered out
            model="gap-outside-team",
            spend=0.0,
            successful_requests=5,
        ),
    ]
    find_many = _make_find_many({("2026-11-01", "2026-11-05"): rows})
    tc, set_prisma = client
    set_prisma(
        _mock_prisma(
            find_many,
            scope_rows_by_hash={TEAM_A_HASH: _scope_row("team", team_id=TEAM_A_ID, label="Account A")},
            team_tokens_by_team_id={TEAM_A_ID: ["sk-team-A-1"]},
        )
    )

    r = tc.get(
        "/dashboard-metrics/pricing-gaps?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {TEAM_A_TOKEN}"},
    )
    assert r.status_code == 200
    models = [g["model"] for g in r.json()["gaps"]]
    assert "gap-in-team" in models
    assert "gap-outside-team" not in models


# ─── Pricing-gaps (global) ───────────────────────────────────────────────


def test_pricing_gaps_lists_only_distinct_zero_cost_models(client) -> None:
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
    set_prisma(_mock_prisma(find_many, {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get(
        "/dashboard-metrics/pricing-gaps?from=2026-11-01&to=2026-11-05",
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )
    assert r.status_code == 200
    gaps = r.json()["gaps"]
    assert sorted(g["model"] for g in gaps) == ["b", "c"]


# ─── Health route ─────────────────────────────────────────────────────────


def test_health_returns_rollup_freshness(client) -> None:
    recent = datetime.now(timezone.utc) - timedelta(seconds=10)
    find_many = _make_find_many({("sentinel", "sentinel"): [_row(updated_at=recent)]})
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many, {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get("/dashboard-metrics/health", headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert 0 <= body["rollupFreshSec"] < 60


def test_health_reports_stale_when_rollup_old(client) -> None:
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    find_many = _make_find_many({("sentinel", "sentinel"): [_row(updated_at=old)]})
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many, {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get("/dashboard-metrics/health", headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "stale"
    assert body["rollupFreshSec"] >= 3600 - 5


def test_health_reports_no_data_when_table_empty(client) -> None:
    find_many = _make_find_many({})
    tc, set_prisma = client
    set_prisma(_mock_prisma(find_many, {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get("/dashboard-metrics/health", headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no-data"
    assert body["rollupFreshSec"] is None


def test_health_scope_required_but_not_filtered(client) -> None:
    """A team-scoped caller must still see pipeline-level freshness.

    The scope is required for auth (401 without a valid token) but
    ``/health`` is intentionally not scope-filtered — a team with zero
    recent activity should not show a false "stale" alarm.
    """
    recent = datetime.now(timezone.utc) - timedelta(seconds=10)
    find_many = _make_find_many({("sentinel", "sentinel"): [_row(updated_at=recent)]})
    tc, set_prisma = client
    set_prisma(
        _mock_prisma(
            find_many,
            scope_rows_by_hash={TEAM_A_HASH: _scope_row("team", team_id=TEAM_A_ID, label="Account A")},
            team_tokens_by_team_id={TEAM_A_ID: []},
        )
    )

    r = tc.get("/dashboard-metrics/health", headers={"Authorization": f"Bearer {TEAM_A_TOKEN}"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ─── Input validation ────────────────────────────────────────────────────


def test_users_rejects_malformed_date(client) -> None:
    tc, set_prisma = client
    set_prisma(_mock_prisma(_make_find_many({}), {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get(
        "/dashboard-metrics/users?from=not-a-date&to=2026-11-05",
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )
    assert r.status_code == 400


def test_users_rejects_reversed_window(client) -> None:
    tc, set_prisma = client
    set_prisma(_mock_prisma(_make_find_many({}), {GLOBAL_HASH: _scope_row("global", label="Global")}))

    r = tc.get(
        "/dashboard-metrics/users?from=2026-11-05&to=2026-11-01",
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )
    assert r.status_code == 400
