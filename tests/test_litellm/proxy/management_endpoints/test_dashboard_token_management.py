"""Tests for the admin-only /team/{team_id}/dashboard-token/* endpoints.

Scope:
  * GET — 403 without proxy admin; 404 when no live token; 200 with metadata.
  * POST — creates and reveals once; 409 when a live row already exists;
    404 when the team doesn't exist.
  * POST /rotate — atomic revoke-old + insert-new via a transaction;
    404 when nothing to rotate.
  * POST /revoke — sets ``revoked_at`` on the live row; 204; 404 when idle.
  * Structured info log lines are emitted for issued / rotated / revoked.

No real database; we mock the Prisma client (including a ``tx()`` async
context manager) and bypass ``user_api_key_auth`` via FastAPI dependency
overrides so the tests don't need a full proxy wiring.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from litellm.proxy._types import LitellmTableNames, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.management_endpoints import dashboard_token_management as dtm
from litellm.proxy.management_endpoints.dashboard_token_management import (
    _LOG_EVENT_ISSUED,
    _LOG_EVENT_REVOKED,
    _LOG_EVENT_ROTATED,
    router as dashboard_token_router,
)

TEAM_ID = "team_01"
TEAM_ALIAS = "Account A"


def _team_row(team_id: str = TEAM_ID, team_alias: str = TEAM_ALIAS) -> SimpleNamespace:
    return SimpleNamespace(team_id=team_id, team_alias=team_alias)


def _token_row(
    token_hash: str = "a" * 64,
    team_id: str = TEAM_ID,
    label: Optional[str] = TEAM_ALIAS,
    revoked_at: Optional[datetime] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        token_hash=token_hash,
        team_id=team_id,
        scope_type="team",
        label=label,
        created_at=datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc),
        created_by="user_admin",
        revoked_at=revoked_at,
    )


def _mock_prisma(
    *,
    team: Optional[SimpleNamespace] = None,
    live_token: Optional[SimpleNamespace] = None,
    create_returns: Optional[SimpleNamespace] = None,
    track_updates: Optional[list[dict]] = None,
    track_creates: Optional[list[dict]] = None,
) -> MagicMock:
    """Build a MagicMock prisma client with the sub-tables these endpoints touch.

    * ``litellm_teamtable.find_first`` → returns ``team`` or None.
    * ``litellm_dashboardtoken.find_first`` → returns ``live_token`` or None.
    * ``litellm_dashboardtoken.create`` / ``.update`` → record the ``data`` /
      ``where`` arg in the given lists; ``create`` returns
      ``create_returns`` if given, else a synthesized row.
    * ``db.tx()`` → async-context-manager yielding an object that mirrors
      ``.litellm_dashboardtoken.update`` and ``.create``, so the rotate
      endpoint can run its transactional block transparently.
    """
    prisma = MagicMock()

    # team table
    team_table = MagicMock()
    team_table.find_first = AsyncMock(return_value=team)
    prisma.db.litellm_teamtable = team_table

    # dashboard token table
    dt_table = MagicMock()
    dt_table.find_first = AsyncMock(return_value=live_token)
    creates = track_creates if track_creates is not None else []
    updates = track_updates if track_updates is not None else []

    async def create(data: dict, **_: Any) -> SimpleNamespace:
        creates.append(data)
        if create_returns is not None:
            return create_returns
        return _token_row(
            token_hash=data["token_hash"],
            team_id=data["team_id"],
            label=data.get("label"),
        )

    async def update(where: dict, data: dict, **_: Any) -> SimpleNamespace:
        updates.append({"where": where, "data": data})
        return SimpleNamespace(**{"token_hash": where.get("token_hash"), **data})

    dt_table.create = create
    dt_table.update = update
    prisma.db.litellm_dashboardtoken = dt_table

    # transaction context manager (used by rotate). The rotate handler's
    # live-row lookup is INSIDE the tx (TOCTOU fix), so the tx-side table
    # needs find_first too.
    @asynccontextmanager
    async def tx_cm():
        tx_table = MagicMock()
        tx_table.litellm_dashboardtoken = MagicMock()
        tx_table.litellm_dashboardtoken.find_first = AsyncMock(return_value=live_token)
        tx_table.litellm_dashboardtoken.create = create
        tx_table.litellm_dashboardtoken.update = update
        yield tx_table

    prisma.db.tx = tx_cm

    return prisma


def _auth_admin() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(
        api_key="sk-admin",
        user_id="user_admin",
        user_role=LitellmUserRoles.PROXY_ADMIN.value,
    )


def _auth_internal_user() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(
        api_key="sk-user",
        user_id="user_42",
        user_role=LitellmUserRoles.INTERNAL_USER.value,
    )


@pytest.fixture
def app_and_prisma(monkeypatch: pytest.MonkeyPatch):
    """Yield (TestClient, set_prisma, set_auth, audit_calls).

    ``audit_calls`` is a list that captures every ``LiteLLM_AuditLogs``
    payload passed to ``create_audit_log_for_update`` from any endpoint
    under test. Tests can introspect ``[c.action for c in audit_calls]``
    etc. to verify audit emission.
    """
    fake_proxy_server = MagicMock()
    fake_proxy_server.prisma_client = None
    fake_proxy_server.litellm_proxy_admin_name = "proxy_admin"
    monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", fake_proxy_server)

    audit_calls: list[Any] = []

    async def _capture_audit(request_data: Any) -> None:
        audit_calls.append(request_data)

    # Patch the symbol imported into the module under test (not the helper's
    # original module), so our shim replaces the one the handlers actually call.
    monkeypatch.setattr(dtm, "create_audit_log_for_update", _capture_audit)

    app = FastAPI()
    app.include_router(dashboard_token_router)

    _auth_holder: dict[str, Any] = {"fn": lambda: _auth_admin()}

    def _overridden_auth():
        return _auth_holder["fn"]()

    app.dependency_overrides[user_api_key_auth] = _overridden_auth

    tc = TestClient(app)

    def set_prisma(mock: MagicMock | None) -> None:
        fake_proxy_server.prisma_client = mock

    def set_auth(fn):
        _auth_holder["fn"] = fn

    yield tc, set_prisma, set_auth, audit_calls


# ─── GET ──────────────────────────────────────────────────────────────────


def test_get_403_when_not_proxy_admin(app_and_prisma) -> None:
    tc, set_prisma, set_auth, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=_team_row()))
    set_auth(_auth_internal_user)

    r = tc.get(f"/team/{TEAM_ID}/dashboard-token")
    assert r.status_code == 403
    assert r.json() == {"detail": {"error": "proxy admin role required"}}


def test_get_404_when_team_missing(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=None))

    r = tc.get(f"/team/{TEAM_ID}/dashboard-token")
    assert r.status_code == 404
    assert "team not found" in r.json()["detail"]["error"]


def test_get_404_when_no_live_token(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=_team_row(), live_token=None))

    r = tc.get(f"/team/{TEAM_ID}/dashboard-token")
    assert r.status_code == 404
    assert r.json() == {"detail": {"error": "no_live_dashboard_token"}}


def test_get_returns_metadata_when_live_token_exists(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    live = _token_row(token_hash="a3f1b8" + "0" * 58)
    set_prisma(_mock_prisma(team=_team_row(), live_token=live))

    r = tc.get(f"/team/{TEAM_ID}/dashboard-token")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == TEAM_ALIAS
    assert body["scope_type"] == "team"
    assert body["team_id"] == TEAM_ID
    assert body["token_hash_prefix"] == "a3f1b800"
    assert body["created_by"] == "user_admin"


# ─── POST (issue) ────────────────────────────────────────────────────────


def test_post_creates_and_returns_raw_token_once(app_and_prisma, caplog) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    created_rows: list[dict] = []
    set_prisma(
        _mock_prisma(team=_team_row(), live_token=None, track_creates=created_rows)
    )

    with caplog.at_level(logging.INFO):
        r = tc.post(f"/team/{TEAM_ID}/dashboard-token", json={"label": "custom"})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["raw_token"].startswith("ldash_")
    assert body["scope_type"] == "team"
    assert body["team_id"] == TEAM_ID
    assert body["label"] == "custom"

    # The stored hash is sha256 of the raw token (verify the DB write).
    expected_hash = hashlib.sha256(body["raw_token"].encode()).hexdigest()
    assert len(created_rows) == 1
    assert created_rows[0]["token_hash"] == expected_hash
    assert created_rows[0]["team_id"] == TEAM_ID
    assert created_rows[0]["scope_type"] == "team"
    assert created_rows[0]["label"] == "custom"
    assert created_rows[0]["created_by"] == "user_admin"

    assert any(
        _LOG_EVENT_ISSUED in record.message for record in caplog.records
    ), "expected dashboard_token.issued log line"

    # Audit DB row — exactly one "created" event, pointing at the new row.
    assert len(audit_calls) == 1
    audit = audit_calls[0]
    assert audit.action == "created"
    assert audit.table_name == LitellmTableNames.DASHBOARD_TOKEN_TABLE_NAME
    assert audit.object_id == expected_hash
    assert audit.changed_by == "user_admin"
    # ``changed_by_api_key`` is sha256-masked by the Pydantic model validator —
    # verify the mask is applied (never carries the raw key).
    assert audit.changed_by_api_key != "sk-admin"
    assert len(audit.changed_by_api_key) == 64


def test_post_defaults_label_to_team_alias_when_body_omits_it(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    created_rows: list[dict] = []
    set_prisma(
        _mock_prisma(team=_team_row(), live_token=None, track_creates=created_rows)
    )

    r = tc.post(f"/team/{TEAM_ID}/dashboard-token", json={})
    assert r.status_code == 201
    assert created_rows[0]["label"] == TEAM_ALIAS


def test_post_409_when_live_token_exists(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=_team_row(), live_token=_token_row()))

    r = tc.post(f"/team/{TEAM_ID}/dashboard-token", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "live_dashboard_token_exists"


def test_post_404_when_team_missing(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=None))

    r = tc.post(f"/team/{TEAM_ID}/dashboard-token", json={})
    assert r.status_code == 404


# ─── POST /rotate ────────────────────────────────────────────────────────


def test_rotate_atomic_revoke_and_create(app_and_prisma, caplog) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    existing = _token_row(token_hash="old-hash" + "0" * 56)
    updates: list[dict] = []
    creates: list[dict] = []
    set_prisma(
        _mock_prisma(
            team=_team_row(),
            live_token=existing,
            track_updates=updates,
            track_creates=creates,
        )
    )

    with caplog.at_level(logging.INFO):
        r = tc.post(f"/team/{TEAM_ID}/dashboard-token/rotate")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["raw_token"].startswith("ldash_")

    # old revoked
    assert len(updates) == 1
    assert updates[0]["where"]["token_hash"] == existing.token_hash
    assert updates[0]["data"]["revoked_at"] is not None

    # new created with matching hash + inherited label
    expected_hash = hashlib.sha256(body["raw_token"].encode()).hexdigest()
    assert len(creates) == 1
    assert creates[0]["token_hash"] == expected_hash
    assert creates[0]["label"] == existing.label

    assert any(
        _LOG_EVENT_ROTATED in record.message for record in caplog.records
    ), "expected dashboard_token.rotated log line"

    # Audit DB row — exactly one "rotated" event, before/after both set.
    assert len(audit_calls) == 1
    audit = audit_calls[0]
    assert audit.action == "rotated"
    assert audit.table_name == LitellmTableNames.DASHBOARD_TOKEN_TABLE_NAME
    assert audit.object_id == expected_hash
    assert audit.changed_by == "user_admin"
    # before/after are dicts in flight, serialized to JSON strings by
    # ``create_audit_log_for_update`` before the DB write. In our mock they
    # arrive as dicts because we patched out the write.
    assert audit.before_value is not None
    assert audit.updated_values is not None


def test_rotate_404_when_no_live_token(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=_team_row(), live_token=None))

    r = tc.post(f"/team/{TEAM_ID}/dashboard-token/rotate")
    assert r.status_code == 404


# ─── POST /revoke ────────────────────────────────────────────────────────


def test_revoke_204_and_marks_row_revoked(app_and_prisma, caplog) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    existing = _token_row(token_hash="live-hash" + "0" * 55)
    updates: list[dict] = []
    set_prisma(
        _mock_prisma(team=_team_row(), live_token=existing, track_updates=updates)
    )

    with caplog.at_level(logging.INFO):
        r = tc.post(f"/team/{TEAM_ID}/dashboard-token/revoke")

    assert r.status_code == 204
    assert len(updates) == 1
    assert updates[0]["where"]["token_hash"] == existing.token_hash
    assert updates[0]["data"]["revoked_at"] is not None

    assert any(
        _LOG_EVENT_REVOKED in record.message for record in caplog.records
    ), "expected dashboard_token.revoked log line"

    # Audit DB row — exactly one "deleted" event (closest fit for revoke).
    assert len(audit_calls) == 1
    audit = audit_calls[0]
    assert audit.action == "deleted"
    assert audit.table_name == LitellmTableNames.DASHBOARD_TOKEN_TABLE_NAME
    assert audit.object_id == existing.token_hash
    assert audit.changed_by == "user_admin"
    assert audit.before_value is not None
    assert audit.updated_values is None


def test_revoke_404_when_no_live_token(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=_team_row(), live_token=None))

    r = tc.post(f"/team/{TEAM_ID}/dashboard-token/revoke")
    assert r.status_code == 404


def test_revoke_403_without_proxy_admin(app_and_prisma) -> None:
    tc, set_prisma, set_auth, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=_team_row(), live_token=_token_row()))
    set_auth(_auth_internal_user)

    r = tc.post(f"/team/{TEAM_ID}/dashboard-token/revoke")
    assert r.status_code == 403
    # No audit emission on auth-rejected mutations.
    assert audit_calls == []


# ─── Audit: no emission on reads or failed mutations ─────────────────────


def test_no_audit_on_get(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=_team_row(), live_token=_token_row()))

    tc.get(f"/team/{TEAM_ID}/dashboard-token")
    assert audit_calls == []


def test_no_audit_on_create_conflict(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=_team_row(), live_token=_token_row()))

    r = tc.post(f"/team/{TEAM_ID}/dashboard-token", json={})
    assert r.status_code == 409
    assert audit_calls == []


def test_no_audit_on_rotate_when_nothing_live(app_and_prisma) -> None:
    tc, set_prisma, _, audit_calls = app_and_prisma
    set_prisma(_mock_prisma(team=_team_row(), live_token=None))

    r = tc.post(f"/team/{TEAM_ID}/dashboard-token/rotate")
    assert r.status_code == 404
    assert audit_calls == []
