"""Connection registry over HTTP: CRUD, bulk import, and secret containment."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from ouroboros import connections as registry


SECRET = "sk-do-not-leak-me-0123456789"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live gateway bound to an isolated data dir."""
    monkeypatch.setenv("OUROBOROS_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path / "data"))
    for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    import ouroboros.config as config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)

    from starlette.applications import Starlette
    from starlette.routing import Route

    from ouroboros.gateway.connections import (
        api_connections_delete,
        api_connections_import,
        api_connections_list,
        api_connections_set_enabled,
        api_connections_upsert,
    )

    app = Starlette(routes=[
        Route("/api/connections", endpoint=api_connections_list, methods=["GET"]),
        Route("/api/connections", endpoint=api_connections_upsert, methods=["POST"]),
        Route("/api/connections/import", endpoint=api_connections_import, methods=["POST"]),
        Route("/api/connections/{connection_id}", endpoint=api_connections_delete, methods=["DELETE"]),
        Route(
            "/api/connections/{connection_id}/enabled",
            endpoint=api_connections_set_enabled,
            methods=["POST"],
        ),
    ])
    with TestClient(app) as test_client:
        yield test_client


def _create(client, connection_id="openai-a", **extra):
    body = {
        "connection_id": connection_id,
        "provider": "openai",
        "kind": "api_key",
        "privacy": "no_training",
        "secret": {"OPENAI_API_KEY": SECRET},
    }
    body.update(extra)
    return client.post("/api/connections", json=body)


def test_create_then_list(client):
    assert _create(client).status_code == 200
    payload = client.get("/api/connections").json()
    rows = payload["connections"]
    assert [row["connection_id"] for row in rows] == ["openai-a"]
    assert rows[0]["credential_configured"] is True
    assert rows[0]["editable"] is True


def test_the_secret_never_appears_in_any_response(client):
    _create(client)
    for response in (
        client.get("/api/connections"),
        _create(client, connection_id="openai-b"),
        client.post("/api/connections/openai-a/enabled", json={"enabled": False}),
    ):
        assert SECRET not in response.text, f"secret leaked in {response.url}"


def test_credential_presence_is_reported_without_the_value(client):
    client.post("/api/connections", json={
        "connection_id": "no-key", "provider": "openai", "kind": "api_key",
    })
    row = next(
        r for r in client.get("/api/connections").json()["connections"]
        if r["connection_id"] == "no-key"
    )
    assert row["credential_configured"] is False


def test_an_invalid_row_is_rejected_with_a_reason(client):
    response = client.post("/api/connections", json={
        "connection_id": "bad id", "provider": "openai",
    })
    assert response.status_code == 400
    assert "connection_id" in response.json().get("error", "")


def test_delete_removes_the_row(client):
    _create(client)
    assert client.delete("/api/connections/openai-a").status_code == 200
    assert client.get("/api/connections").json()["connections"] == []


def test_enable_toggle_round_trips(client):
    _create(client)
    client.post("/api/connections/openai-a/enabled", json={"enabled": False})
    row = client.get("/api/connections").json()["connections"][0]
    assert row["enabled"] is False


def test_bulk_import_reports_partial_success_instead_of_failing_wholesale(client):
    response = client.post("/api/connections/import", json={"connections": [
        {"connection_id": "k1", "provider": "openai", "secret": {"OPENAI_API_KEY": SECRET}},
        {"connection_id": "", "provider": "openai"},
        {"connection_id": "k2", "provider": "openai", "secret": {"OPENAI_API_KEY": SECRET}},
        {"connection_id": "k3", "provider": "openai", "privacy": "maybe"},
    ]})
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == ["k1", "k2"]
    assert [item["index"] for item in body["rejected"]] == [1, 3]
    assert SECRET not in response.text


def test_bulk_import_refuses_an_unbounded_body(client):
    rows = [{"connection_id": f"k{i}", "provider": "openai"} for i in range(1001)]
    response = client.post("/api/connections/import", json={"connections": rows})
    assert response.status_code == 400


def test_a_legacy_projected_row_is_listed_but_not_editable(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-settings")
    rows = client.get("/api/connections").json()["connections"]
    legacy = [row for row in rows if row["connection_id"] == "legacy:openai"]
    assert len(legacy) == 1
    assert legacy[0]["editable"] is False
    # Its value comes from the settings key, so it must not be echoed either.
    assert "sk-from-settings" not in client.get("/api/connections").text


def test_storing_a_connection_writes_no_secret_into_the_catalog_file(client, tmp_path):
    _create(client)
    catalog = registry.catalog_path(tmp_path / "data").read_text(encoding="utf-8")
    assert SECRET not in catalog
    assert json.loads(catalog)["connections"][0]["connection_id"] == "openai-a"
