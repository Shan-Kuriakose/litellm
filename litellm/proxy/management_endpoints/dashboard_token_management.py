"""Admin-only CRUD for ``LiteLLM_DashboardToken`` team rows.

Four endpoints, all gated by ``user_api_key_auth`` + an inline proxy-admin
role check. They are called from the LiteLLM admin UI, never from a
dashboard — hence they use the same auth model as ``/key/generate`` and
NOT the ``dashboard_bearer_token_auth`` the data endpoints use.

Routes (relative to the proxy base URL):
  GET  /team/{team_id}/dashboard-token          — read metadata
  POST /team/{team_id}/dashboard-token          — issue a new token
  POST /team/{team_id}/dashboard-token/rotate   — atomic revoke+insert
  POST /team/{team_id}/dashboard-token/revoke   — revoke the live token

All mutations emit a structured info log for lifecycle visibility:
  * ``dashboard_token.issued`` — new live row created
  * ``dashboard_token.rotated`` — old revoked + new live, in one txn
  * ``dashboard_token.revoked`` — live row revoked without replacement

The raw ``ldash_…`` token is returned exactly once (at issue / rotate) and
is never persisted anywhere other than the admin's local storage — the
database carries only ``sha256(raw).hexdigest()``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Annotated, Any, Final, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from pydantic import BaseModel

from litellm._logging import verbose_proxy_logger
from litellm._uuid import uuid
from litellm.proxy._types import (
    AUDIT_ACTIONS,
    LiteLLM_AuditLogs,
    LitellmTableNames,
    LitellmUserRoles,
    UserAPIKeyAuth,
)
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.management_helpers.audit_logs import (
    create_audit_log_for_update,
    get_audit_log_changed_by,
)

router: Final = APIRouter(tags=["dashboard-token-admin"])

_TOKEN_PREFIX: Final = "ldash_"
_TOKEN_BYTES: Final = 32  # => 43-char base64url body
_LOG_EVENT_ISSUED: Final = "dashboard_token.issued"
_LOG_EVENT_ROTATED: Final = "dashboard_token.rotated"
_LOG_EVENT_REVOKED: Final = "dashboard_token.revoked"


def _audit_task_done(task: asyncio.Task) -> None:
    """Surface any exception from the fire-and-forget audit task.

    Without this callback, an exception in ``create_audit_log_for_update``
    would be silently swallowed by asyncio. We log at warning so the
    operational signal is preserved; the HTTP response has already been
    returned so the user is unaffected.
    """
    try:
        exc = task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        return
    if exc is not None:
        verbose_proxy_logger.warning(
            "dashboard-token audit log task failed: %s", exc
        )


# ─── Response models ─────────────────────────────────────────────────────


class DashboardTokenMetadata(BaseModel):
    """Public metadata safe to return for an existing token."""

    label: Optional[str]
    scope_type: str
    team_id: Optional[str]
    created_at: str
    created_by: Optional[str]
    token_hash_prefix: str  # first 8 hex chars; identifies the row in logs without leaking


class DashboardTokenReveal(BaseModel):
    """One-time payload returned from POST / POST rotate.

    ``raw_token`` is included here and nowhere else in the system. The admin
    must store it immediately; subsequent GETs return metadata only.
    """

    raw_token: str
    label: Optional[str]
    scope_type: str
    team_id: Optional[str]
    created_at: str
    warning: str = "Store this token now. It will not be shown again."


class _IssueBody(BaseModel):
    label: Optional[str] = None


# ─── Helpers ─────────────────────────────────────────────────────────────


def _require_proxy_admin(user_api_key_dict: UserAPIKeyAuth) -> None:
    if user_api_key_dict.user_role != LitellmUserRoles.PROXY_ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "proxy admin role required"},
        )


def _get_prisma_client() -> Any:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "database not connected"},
        )
    return prisma_client


async def _assert_team_exists(prisma_client: Any, team_id: str) -> Any:
    team = await prisma_client.db.litellm_teamtable.find_first(
        where={"team_id": team_id}
    )
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"team not found: {team_id}"},
        )
    return team


async def _get_live_team_row(prisma_client: Any, team_id: str) -> Optional[Any]:
    return await prisma_client.db.litellm_dashboardtoken.find_first(
        where={
            "team_id": team_id,
            "scope_type": "team",
            "revoked_at": None,
        }
    )


def _generate_raw_token() -> str:
    return f"{_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _as_iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _metadata_from_row(row: Any) -> DashboardTokenMetadata:
    return DashboardTokenMetadata(
        label=row.label,
        scope_type=row.scope_type,
        team_id=row.team_id,
        created_at=_as_iso(row.created_at),
        created_by=row.created_by,
        token_hash_prefix=str(row.token_hash)[:8],
    )


def _reveal_from_row(row: Any, raw_token: str) -> DashboardTokenReveal:
    return DashboardTokenReveal(
        raw_token=raw_token,
        label=row.label,
        scope_type=row.scope_type,
        team_id=row.team_id,
        created_at=_as_iso(row.created_at),
    )


def _emit_audit_event(
    *,
    action: AUDIT_ACTIONS,
    token_hash: str,
    user_api_key_dict: UserAPIKeyAuth,
    before: Optional[dict],
    after: Optional[dict],
) -> None:
    """Fire-and-forget audit log write.

    Mirrors the pattern used by key/team management endpoints — emits a
    ``LiteLLM_AuditLogs`` row via ``create_audit_log_for_update``, wrapped in
    ``asyncio.create_task`` so the HTTP response is never blocked by the
    audit write. ``create_audit_log_for_update`` internally short-circuits
    when ``is_audit_logging_enabled()`` returns False, so this call is safe
    even on free-tier deployments.

    ``object_id`` is the full ``token_hash`` (64 hex chars) — unique and
    stable across the row's lifecycle. Callers pass the before/after state
    explicitly; pass ``None`` to signal "no prior state" (create) or
    "logically deleted" (revoke).
    """
    from litellm.proxy.proxy_server import litellm_proxy_admin_name

    # LiteLLM_AuditLogs' before_value / updated_values are typed ``Json``
    # (serialized strings). ``create_audit_log_for_update`` does accept dict
    # shapes and json-dumps them before the DB write, but Pydantic validates
    # on construction — so we serialize eagerly to satisfy both.
    payload = LiteLLM_AuditLogs(
        id=str(uuid.uuid4()),
        updated_at=datetime.now(timezone.utc),
        changed_by=get_audit_log_changed_by(
            litellm_changed_by=None,
            user_api_key_dict=user_api_key_dict,
            litellm_proxy_admin_name=litellm_proxy_admin_name,
        ),
        changed_by_api_key=user_api_key_dict.api_key,
        table_name=LitellmTableNames.DASHBOARD_TOKEN_TABLE_NAME,
        object_id=token_hash,
        action=action,
        before_value=json.dumps(before, default=str) if before is not None else None,
        updated_values=json.dumps(after, default=str) if after is not None else None,
    )
    task = asyncio.create_task(create_audit_log_for_update(request_data=payload))
    task.add_done_callback(_audit_task_done)


# ─── Routes ──────────────────────────────────────────────────────────────


@router.get(
    "/team/{team_id}/dashboard-token",
    response_model=DashboardTokenMetadata,
)
async def get_team_dashboard_token(
    team_id: Annotated[str, Path()],
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> DashboardTokenMetadata:
    _require_proxy_admin(user_api_key_dict)
    prisma_client = _get_prisma_client()
    await _assert_team_exists(prisma_client, team_id)

    row = await _get_live_team_row(prisma_client, team_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_live_dashboard_token"},
        )
    return _metadata_from_row(row)


@router.post(
    "/team/{team_id}/dashboard-token",
    response_model=DashboardTokenReveal,
    status_code=status.HTTP_201_CREATED,
)
async def create_team_dashboard_token(
    team_id: Annotated[str, Path()],
    body: Annotated[_IssueBody, Body()] = _IssueBody(),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> DashboardTokenReveal:
    _require_proxy_admin(user_api_key_dict)
    prisma_client = _get_prisma_client()
    team = await _assert_team_exists(prisma_client, team_id)

    if await _get_live_team_row(prisma_client, team_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "live_dashboard_token_exists",
                "hint": "rotate or revoke the existing token first",
            },
        )

    label = body.label or getattr(team, "team_alias", None)
    raw_token = _generate_raw_token()
    try:
        row = await prisma_client.db.litellm_dashboardtoken.create(
            data={
                "token_hash": _hash(raw_token),
                "team_id": team_id,
                "scope_type": "team",
                "label": label,
                "created_by": user_api_key_dict.user_id,
            }
        )
    except Exception as create_exc:
        # Two admins may have raced the pre-check → .create. The partial
        # unique index ``one_live_per_team`` will have rejected the loser.
        # Re-check live-row state; if it's now present, surface the proper
        # 409 instead of the generic 500 the outer handler would emit.
        if await _get_live_team_row(prisma_client, team_id) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "live_dashboard_token_exists",
                    "hint": "rotate or revoke the existing token first",
                },
            ) from create_exc
        raise
    verbose_proxy_logger.info(
        "%s team_id=%s token_hash_prefix=%s created_by=%s",
        _LOG_EVENT_ISSUED,
        team_id,
        str(row.token_hash)[:8],
        user_api_key_dict.user_id,
    )
    _emit_audit_event(
        action="created",
        token_hash=str(row.token_hash),
        user_api_key_dict=user_api_key_dict,
        before=None,
        after={
            "team_id": team_id,
            "label": label,
            "token_hash_prefix": str(row.token_hash)[:8],
        },
    )
    return _reveal_from_row(row, raw_token)


@router.post(
    "/team/{team_id}/dashboard-token/rotate",
    response_model=DashboardTokenReveal,
)
async def rotate_team_dashboard_token(
    team_id: Annotated[str, Path()],
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> DashboardTokenReveal:
    _require_proxy_admin(user_api_key_dict)
    prisma_client = _get_prisma_client()
    await _assert_team_exists(prisma_client, team_id)

    raw_token = _generate_raw_token()
    new_hash = _hash(raw_token)
    now = datetime.now(timezone.utc)

    # Atomic: fetch live row + revoke it + insert new, all in one transaction.
    # The live-row lookup lives INSIDE the tx so a concurrent rotate can't
    # race us (TOCTOU) and leave stale state. The partial unique index
    # ``one_live_per_team`` is the DB-level safety net; this layering closes
    # the Python-level race window too.
    async with prisma_client.db.tx() as tx:
        existing = await tx.litellm_dashboardtoken.find_first(
            where={
                "team_id": team_id,
                "scope_type": "team",
                "revoked_at": None,
            }
        )
        if existing is None:
            # Nothing to rotate — surfaces 404. Tx rolls back cleanly.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "no_live_dashboard_token"},
            )

        # Carry the label forward so rotation preserves the human-readable name.
        label = existing.label

        await tx.litellm_dashboardtoken.update(
            where={"token_hash": existing.token_hash},
            data={"revoked_at": now},
        )
        row = await tx.litellm_dashboardtoken.create(
            data={
                "token_hash": new_hash,
                "team_id": team_id,
                "scope_type": "team",
                "label": label,
                "created_by": user_api_key_dict.user_id,
            }
        )

    verbose_proxy_logger.info(
        "%s team_id=%s old_token_hash_prefix=%s new_token_hash_prefix=%s rotated_by=%s",
        _LOG_EVENT_ROTATED,
        team_id,
        str(existing.token_hash)[:8],
        str(row.token_hash)[:8],
        user_api_key_dict.user_id,
    )
    _emit_audit_event(
        action="rotated",
        token_hash=str(row.token_hash),
        user_api_key_dict=user_api_key_dict,
        before={
            "team_id": team_id,
            "label": existing.label,
            "token_hash_prefix": str(existing.token_hash)[:8],
        },
        after={
            "team_id": team_id,
            "label": label,
            "token_hash_prefix": str(row.token_hash)[:8],
        },
    )
    return _reveal_from_row(row, raw_token)


@router.post(
    "/team/{team_id}/dashboard-token/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_team_dashboard_token(
    team_id: Annotated[str, Path()],
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> None:
    _require_proxy_admin(user_api_key_dict)
    prisma_client = _get_prisma_client()
    await _assert_team_exists(prisma_client, team_id)

    existing = await _get_live_team_row(prisma_client, team_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_live_dashboard_token"},
        )

    await prisma_client.db.litellm_dashboardtoken.update(
        where={"token_hash": existing.token_hash},
        data={"revoked_at": datetime.now(timezone.utc)},
    )
    verbose_proxy_logger.info(
        "%s team_id=%s token_hash_prefix=%s revoked_by=%s",
        _LOG_EVENT_REVOKED,
        team_id,
        str(existing.token_hash)[:8],
        user_api_key_dict.user_id,
    )
    # "deleted" is the closest fit in the existing AUDIT_ACTIONS literal —
    # the row's ``revoked_at`` is set, so it is no longer live/usable.
    _emit_audit_event(
        action="deleted",
        token_hash=str(existing.token_hash),
        user_api_key_dict=user_api_key_dict,
        before={
            "team_id": team_id,
            "label": existing.label,
            "token_hash_prefix": str(existing.token_hash)[:8],
            "created_at": _as_iso(existing.created_at),
        },
        after=None,
    )
    return None
