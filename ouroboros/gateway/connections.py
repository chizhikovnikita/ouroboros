"""HTTP surface for the connection registry.

Transport only — every rule about identity, validation and storage lives in
``ouroboros.connections``. The one thing this layer owns is the guarantee that a
credential never leaves the process: responses are built from
``Connection.to_public_dict`` (which carries no secret by construction) plus a
boolean "configured" flag, never from the secret store itself.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros import connections as registry
from ouroboros.gateway._helpers import json_error, request_json_or


log = logging.getLogger(__name__)

# Bulk import is the point of the feature (hundreds of keys), but an unbounded
# body would let one paste exhaust memory before validation ever runs.
_MAX_IMPORT_ROWS = 1000


def _row_payload(connection: registry.Connection, stats: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Catalog row + credential PRESENCE + measured rating. No secret, ever."""
    row = connection.to_public_dict()
    row["credential_configured"] = registry.has_credential(connection)
    row["editable"] = connection.origin != registry.ORIGIN_LEGACY
    # `rating` stays null for a connection nobody has used yet — "not measured"
    # is a different statement from "measured badly", and the UI must show it as
    # such rather than as a zero.
    row["rating"] = (stats or {}).get(connection.connection_id)
    return row


async def api_connections_list(request: Request) -> JSONResponse:
    """GET /api/connections — the catalog plus the legacy projection."""
    try:
        from ouroboros.connection_rating import collect_stats

        try:
            stats = {cid: item.to_public_dict() for cid, item in collect_stats().items()}
        except Exception:
            # Ratings are an overlay on the catalog; losing them must not take the
            # page down with them.
            log.warning("connection ratings unavailable", exc_info=True)
            stats = {}
        rows = [_row_payload(conn, stats) for conn in registry.all_connections()]
    except Exception as exc:  # storage-level failure must not 500 the whole UI
        log.warning("connections list failed", exc_info=True)
        return json_error(f"could not read the connection registry: {exc}", 500)
    return JSONResponse({
        "connections": rows,
        "kinds": sorted(registry.VALID_KINDS),
        "privacy_values": sorted(registry.VALID_PRIVACY),
    })


async def api_connections_upsert(request: Request) -> JSONResponse:
    """POST /api/connections — create or replace one connection."""
    body = await request_json_or(request, {})
    if not isinstance(body, dict):
        return json_error("body must be an object", 400)

    secret_values = body.pop("secret", None)
    try:
        connection = registry.parse_connection(body, where="connection")
        registry.upsert(connection)
        if isinstance(secret_values, dict) and secret_values:
            registry.set_secret(connection.connection_id, secret_values)
    except registry.ConnectionConfigError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:
        log.warning("connection upsert failed", exc_info=True)
        return json_error(f"could not store the connection: {exc}", 500)

    return JSONResponse({"connection": _row_payload(connection)})


async def api_connections_delete(request: Request) -> JSONResponse:
    """DELETE /api/connections/{connection_id} — row and stored secret."""
    connection_id = str(request.path_params.get("connection_id") or "")
    if not connection_id:
        return json_error("connection_id is required", 400)
    try:
        registry.remove(connection_id)
    except Exception as exc:
        log.warning("connection delete failed", exc_info=True)
        return json_error(f"could not delete the connection: {exc}", 500)
    return JSONResponse({"deleted": connection_id})


async def api_connections_import(request: Request) -> JSONResponse:
    """POST /api/connections/import — many rows at once.

    Partial success is REPORTED, not hidden: a bad row is returned with its reason
    and index so a 300-key paste does not fail wholesale on one typo, and the owner
    can see exactly which lines were skipped.
    """
    body = await request_json_or(request, {})
    rows = body.get("connections") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return json_error("connections[] is required", 400)
    if len(rows) > _MAX_IMPORT_ROWS:
        return json_error(f"at most {_MAX_IMPORT_ROWS} connections per import", 400)

    added: List[str] = []
    rejected: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        secret_values = None
        if isinstance(row, dict):
            row = dict(row)
            secret_values = row.pop("secret", None)
        try:
            connection = registry.parse_connection(row, where=f"connections[{index}]")
            registry.upsert(connection)
            if isinstance(secret_values, dict) and secret_values:
                registry.set_secret(connection.connection_id, secret_values)
            added.append(connection.connection_id)
        except registry.ConnectionConfigError as exc:
            rejected.append({"index": index, "error": str(exc)})
        except Exception as exc:
            log.warning("connection import row %d failed", index, exc_info=True)
            rejected.append({"index": index, "error": str(exc)})

    return JSONResponse({"added": added, "rejected": rejected, "added_count": len(added)})


async def api_connections_set_enabled(request: Request) -> JSONResponse:
    """POST /api/connections/{connection_id}/enabled — toggle without a full edit."""
    connection_id = str(request.path_params.get("connection_id") or "")
    body = await request_json_or(request, {})
    enabled = bool(body.get("enabled", True)) if isinstance(body, dict) else True
    try:
        registry.set_enabled(connection_id, enabled)
    except Exception as exc:
        log.warning("connection enable toggle failed", exc_info=True)
        return json_error(f"could not update the connection: {exc}", 500)
    return JSONResponse({"connection_id": connection_id, "enabled": enabled})
