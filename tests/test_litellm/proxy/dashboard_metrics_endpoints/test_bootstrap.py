"""Tests for the global dashboard-token bootstrap hook.

Scope:
  * ``DASHBOARD_METRICS_TOKEN`` unset → no DB calls, returns cleanly.
  * Env set, table empty → inserts the row with the correct hash + fields.
  * Env set, matching hash already present → idempotent (no insert).
  * Env set, different live global hash present → logs warning, no insert.
  * Env set but DB raises on query → logged, no propagation (fail-open).

No real database; the ``prisma_client`` is a MagicMock so the hook can be
exercised without the full proxy server.
"""

from __future__ import annotations

import hashlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from litellm.proxy.dashboard_metrics_endpoints.bootstrap import (
    DASHBOARD_METRICS_TOKEN_ENV_VAR,
    seed_global_dashboard_token,
)

RAW_TOKEN = "test-dashboard-token-0123456789abcdef"
TOKEN_HASH = hashlib.sha256(RAW_TOKEN.encode()).hexdigest()


def _mock_prisma_client(
    *,
    matching_existing: object | None = None,
    any_live_existing: object | None = None,
) -> MagicMock:
    """Build a MagicMock prisma_client where ``find_first`` yields the given rows in order.

    The hook calls ``find_first`` twice: once scoped to the matching hash, then
    (if that returns None) once scoped to any live global row. Returning
    ``[matching_existing, any_live_existing]`` from side_effect lets each test
    simulate a specific table state.
    """
    client = MagicMock()
    client.db = MagicMock()
    client.db.litellm_dashboardtoken = MagicMock()
    client.db.litellm_dashboardtoken.find_first = AsyncMock(
        side_effect=[matching_existing, any_live_existing]
    )
    client.db.litellm_dashboardtoken.create = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_bootstrap_noop_when_env_unset(monkeypatch, caplog):
    monkeypatch.delenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, raising=False)
    client = _mock_prisma_client()

    with caplog.at_level(logging.INFO):
        await seed_global_dashboard_token(client)

    client.db.litellm_dashboardtoken.find_first.assert_not_called()
    client.db.litellm_dashboardtoken.create.assert_not_called()


@pytest.mark.asyncio
async def test_bootstrap_inserts_when_table_empty(monkeypatch):
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, RAW_TOKEN)
    client = _mock_prisma_client(matching_existing=None, any_live_existing=None)

    await seed_global_dashboard_token(client)

    client.db.litellm_dashboardtoken.create.assert_awaited_once()
    create_call = client.db.litellm_dashboardtoken.create.await_args
    data = create_call.kwargs["data"]
    assert data["token_hash"] == TOKEN_HASH
    assert data["team_id"] is None
    assert data["scope_type"] == "global"
    assert data["label"] == "Global"
    assert data["created_by"] == "bootstrap"


@pytest.mark.asyncio
async def test_bootstrap_idempotent_when_matching_hash_present(monkeypatch):
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, RAW_TOKEN)
    existing = SimpleNamespace(
        token_hash=TOKEN_HASH, scope_type="global", team_id=None, revoked_at=None
    )
    client = _mock_prisma_client(matching_existing=existing)

    await seed_global_dashboard_token(client)

    client.db.litellm_dashboardtoken.create.assert_not_called()


@pytest.mark.asyncio
async def test_bootstrap_warns_and_noop_when_different_live_global(monkeypatch, caplog):
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, RAW_TOKEN)
    different_hash = hashlib.sha256(b"a-different-raw-token").hexdigest()
    different_live = SimpleNamespace(
        token_hash=different_hash, scope_type="global", team_id=None, revoked_at=None
    )
    client = _mock_prisma_client(
        matching_existing=None, any_live_existing=different_live
    )

    with caplog.at_level(logging.WARNING):
        await seed_global_dashboard_token(client)

    client.db.litellm_dashboardtoken.create.assert_not_called()
    assert any(
        "live global dashboard token already exists" in record.message
        for record in caplog.records
    ), "expected warning about existing live global row"


@pytest.mark.asyncio
async def test_bootstrap_multi_worker_race_is_idempotent_success(monkeypatch, caplog):
    """A concurrent worker winning the create must NOT log 'bootstrap failed'.

    Simulates: both pre-checks see table empty, both call ``.create``, the
    partial unique index rejects ours. Re-checking the table shows the row IS
    present under our hash (the other worker won). The function must treat
    that as idempotent-success (INFO), not a real failure (WARNING).
    """
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, RAW_TOKEN)

    client = MagicMock()
    client.db = MagicMock()
    client.db.litellm_dashboardtoken = MagicMock()

    # Pre-checks (2 calls): both return None (table empty from our view).
    # Post-create re-check (1 call): returns the row — the concurrent worker
    # inserted it between our check and our create.
    winning_row = SimpleNamespace(
        token_hash=TOKEN_HASH, scope_type="global", team_id=None, revoked_at=None
    )
    client.db.litellm_dashboardtoken.find_first = AsyncMock(
        side_effect=[None, None, winning_row]
    )
    # Our .create loses the race — unique-violation.
    client.db.litellm_dashboardtoken.create = AsyncMock(
        side_effect=Exception("duplicate key value violates unique constraint")
    )

    with caplog.at_level(logging.INFO):
        await seed_global_dashboard_token(client)

    # No misleading "bootstrap failed" WARNING.
    assert not any(
        "bootstrap failed" in record.message and record.levelno >= logging.WARNING
        for record in caplog.records
    ), "race-loser must not log a bootstrap failure warning"

    # Should log at INFO that the row is already seeded.
    assert any(
        "idempotent-success" in record.message or "already seeded" in record.message
        for record in caplog.records
    ), "expected info log about the race winner's row"


@pytest.mark.asyncio
async def test_bootstrap_fail_open_on_db_exception(monkeypatch, caplog):
    monkeypatch.setenv(DASHBOARD_METRICS_TOKEN_ENV_VAR, RAW_TOKEN)
    client = MagicMock()
    client.db = MagicMock()
    client.db.litellm_dashboardtoken = MagicMock()
    client.db.litellm_dashboardtoken.find_first = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    client.db.litellm_dashboardtoken.create = AsyncMock()

    with caplog.at_level(logging.WARNING):
        # Must not raise.
        await seed_global_dashboard_token(client)

    client.db.litellm_dashboardtoken.create.assert_not_called()
    assert any(
        "bootstrap failed" in record.message for record in caplog.records
    ), "expected warning log about bootstrap failure"
