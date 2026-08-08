"""Connection selection: load spreading, limits, cooldown, and the no-op overlay."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from ouroboros import connection_pool as pool
from ouroboros import connections as conns
from ouroboros.usage_accounting import AttemptRequest, reserve_attempt


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    pool.reset_cooldowns()
    yield
    pool.reset_cooldowns()


def _add(tmp_path, connection_id, **extra):
    row = {"connection_id": connection_id, "provider": "openai", "kind": "api_key"}
    row.update(extra)
    conns.upsert(conns.parse_connection(row), tmp_path)
    conns.set_secret(connection_id, {"OPENAI_API_KEY": f"sk-{connection_id}"}, tmp_path)


def _reserve(tmp_path, connection_id, model="openai::gpt-x"):
    return reserve_attempt(AttemptRequest(
        model=model,
        provider="openai",
        connection_id=connection_id,
        drive_root=tmp_path,
        global_limit_usd=1000.0,
        max_budget_usd=0.01,
    ))


# --- selection ---------------------------------------------------------------


def test_no_registry_means_no_opinion(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert pool.select("openai::gpt-x", tmp_path) is None


def test_a_lone_legacy_key_stays_on_the_environment_path(tmp_path, monkeypatch):
    # Rebuilding an identical target through the overlay would add risk for no
    # gain; the single-provider install must keep using exactly the old path.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert pool.select("openai::gpt-x", tmp_path) is None


def test_a_registered_connection_is_selected_with_its_own_credential(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _add(tmp_path, "key-a")
    _add(tmp_path, "key-b")
    selection = pool.select("openai::gpt-x", tmp_path)
    assert selection is not None
    assert selection.connection_id in {"key-a", "key-b"}
    assert selection.credentials["OPENAI_API_KEY"] == f"sk-{selection.connection_id}"


def test_selection_spreads_across_an_idle_pool(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for name in ("key-a", "key-b", "key-c"):
        _add(tmp_path, name)
    seen = {pool.select("openai::gpt-x", tmp_path).connection_id for _ in range(60)}
    # A stable order would pin every idle-pool call to one connection, which is
    # exactly the masseless behavior this feature exists to remove.
    assert seen == {"key-a", "key-b", "key-c"}


def test_excluded_connections_are_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _add(tmp_path, "key-a")
    _add(tmp_path, "key-b")
    for _ in range(20):
        assert pool.select("openai::gpt-x", tmp_path, exclude=["key-a"]).connection_id == "key-b"


# --- in-flight, derived from the ledger --------------------------------------


def test_in_flight_is_counted_from_reserved_ledger_rows(tmp_path):
    assert pool.in_flight_counts(tmp_path) == {}
    _reserve(tmp_path, "key-a")
    _reserve(tmp_path, "key-a")
    _reserve(tmp_path, "key-b")
    assert pool.in_flight_counts(tmp_path) == {"key-a": 2, "key-b": 1}


def test_a_dispatched_attempt_still_counts_as_in_flight(tmp_path):
    # In flight means "the provider may still be working on it", which covers both
    # reserved and dispatched — only a terminal row frees the slot.
    from ouroboros.usage_accounting import mark_dispatched

    reservation = _reserve(tmp_path, "key-a")
    mark_dispatched(reservation)
    assert pool.in_flight_counts(tmp_path).get("key-a") == 1


def test_a_settled_attempt_stops_counting_as_in_flight(tmp_path):
    from ouroboros.usage_accounting import mark_dispatched, settle_attempt

    reservation = _reserve(tmp_path, "key-a")
    mark_dispatched(reservation)
    assert pool.in_flight_counts(tmp_path).get("key-a") == 1
    settle_attempt(reservation, {"prompt_tokens": 1, "completion_tokens": 1}, cost_usd=0.0, cost_final=True)
    assert pool.in_flight_counts(tmp_path).get("key-a", 0) == 0


def test_in_flight_is_visible_across_processes(tmp_path):
    """The load signal must survive the worker-per-process model.

    An in-memory counter would read zero here, which is the whole reason this is
    derived from the ledger instead of stored.
    """
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {os.getcwd()!r})
        from ouroboros.usage_accounting import AttemptRequest, reserve_attempt
        reserve_attempt(AttemptRequest(
            model="openai::gpt-x", provider="openai", connection_id="from-child",
            drive_root={str(tmp_path)!r}, global_limit_usd=1000.0, max_budget_usd=0.01,
        ))
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr

    assert pool.in_flight_counts(tmp_path).get("from-child") == 1


def test_selection_prefers_the_least_loaded_connection(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _add(tmp_path, "busy")
    _add(tmp_path, "idle")
    for _ in range(3):
        _reserve(tmp_path, "busy")
    for _ in range(10):
        assert pool.select("openai::gpt-x", tmp_path).connection_id == "idle"


def test_a_connection_at_its_concurrency_ceiling_is_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _add(tmp_path, "capped", max_concurrent=1)
    _add(tmp_path, "free")
    _reserve(tmp_path, "capped")
    for _ in range(10):
        assert pool.select("openai::gpt-x", tmp_path).connection_id == "free"


def test_every_connection_capped_still_routes_rather_than_failing(tmp_path, monkeypatch):
    # Refusing here would fail a send the provider might well accept.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _add(tmp_path, "only", max_concurrent=1)
    _reserve(tmp_path, "only")
    assert pool.select("openai::gpt-x", tmp_path).connection_id == "only"


def test_an_unreadable_ledger_degrades_to_no_load_awareness(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr("ouroboros.usage_ledger._read_records_locked", _boom)
    assert pool.in_flight_counts(tmp_path) == {}


# --- cooldown / failover -----------------------------------------------------


def test_a_failed_connection_leaves_rotation_and_traffic_switches(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _add(tmp_path, "broken")
    _add(tmp_path, "healthy")
    pool.note_failure("broken")
    assert pool.cooling_down("broken")
    for _ in range(10):
        assert pool.select("openai::gpt-x", tmp_path).connection_id == "healthy"


def test_success_clears_a_cooldown(tmp_path):
    pool.note_failure("key-a")
    assert pool.cooling_down("key-a")
    pool.note_success("key-a")
    assert not pool.cooling_down("key-a")


def test_a_bad_credential_rests_longer_than_a_transient_error(monkeypatch):
    recorded = {}
    monkeypatch.setattr(pool, "_now", lambda: 1000.0)
    pool.note_failure("transient", kind="error")
    recorded["transient"] = pool._COOLDOWN_UNTIL["transient"]
    pool.note_failure("bad-key", kind="auth")
    recorded["auth"] = pool._COOLDOWN_UNTIL["bad-key"]
    assert recorded["auth"] > recorded["transient"]


def test_when_every_candidate_is_resting_one_is_still_tried(tmp_path, monkeypatch):
    # All-resting must not mean "cannot route": the alternative is failing a task
    # over a cooldown that is only a hint.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _add(tmp_path, "a")
    _add(tmp_path, "b")
    pool.note_failure("a")
    pool.note_failure("b")
    assert pool.select("openai::gpt-x", tmp_path) is not None


# --- overlay onto the provider target ----------------------------------------


def test_the_overlay_is_a_no_op_without_a_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path, raising=False)
    target = {"provider": "openai", "api_key": "sk-env", "base_url": "https://api.openai.com/v1"}
    assert pool.apply_to_target(dict(target), "openai::gpt-x", tmp_path) == target


def test_the_overlay_replaces_the_credential_and_stamps_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _add(tmp_path, "key-a")
    _add(tmp_path, "key-b")
    target = {"provider": "openai", "api_key": "sk-env", "base_url": "https://api.openai.com/v1"}
    updated = pool.apply_to_target(dict(target), "openai::gpt-x", tmp_path)
    assert updated["connection_id"] in {"key-a", "key-b"}
    assert updated["api_key"] == f"sk-{updated['connection_id']}"
    # Everything the resolver knew about the provider survives untouched.
    assert updated["base_url"] == target["base_url"]


def test_an_endpoint_connection_overrides_the_base_url(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    conns.upsert(conns.parse_connection({
        "connection_id": "local-1", "provider": "openai-compatible", "kind": "endpoint",
        "base_url": "http://127.0.0.1:9001/v1",
    }), tmp_path)
    conns.upsert(conns.parse_connection({
        "connection_id": "local-2", "provider": "openai-compatible", "kind": "endpoint",
        "base_url": "http://127.0.0.1:9002/v1",
    }), tmp_path)
    target = {"provider": "openai-compatible", "api_key": "", "base_url": "http://old/v1"}
    updated = pool.apply_to_target(dict(target), "openai-compatible::llama", tmp_path)
    assert updated["base_url"] in {"http://127.0.0.1:9001/v1", "http://127.0.0.1:9002/v1"}


def test_the_pool_can_be_switched_off_entirely(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_DISABLE_CONNECTION_POOL", "1")
    _add(tmp_path, "key-a")
    target = {"provider": "openai", "api_key": "sk-env"}
    assert pool.apply_to_target(dict(target), "openai::gpt-x", tmp_path) == target


def test_a_selection_for_another_provider_never_overwrites_the_target(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _add(tmp_path, "openai-only")
    target = {"provider": "anthropic", "api_key": "sk-anthropic-env"}
    assert pool.apply_to_target(dict(target), "anthropic::claude", tmp_path)["api_key"] == "sk-anthropic-env"


# --- the health signal is actually fed from the real send path ----------------


def test_a_failing_send_puts_its_connection_into_cooldown(tmp_path):
    """The cooldown machinery is worthless unless the real send path feeds it."""
    from ouroboros.usage_accounting import execute_physical_attempt

    request = AttemptRequest(
        model="openai::gpt-x", provider="openai", connection_id="key-a",
        drive_root=tmp_path, global_limit_usd=1000.0, max_budget_usd=0.01,
    )

    def _boom():
        raise RuntimeError("503 upstream unavailable")

    with pytest.raises(RuntimeError):
        execute_physical_attempt(request, _boom)
    assert pool.cooling_down("key-a")


def test_an_auth_failure_is_separated_from_a_transient_one(tmp_path, monkeypatch):
    from ouroboros.usage_accounting import execute_physical_attempt

    monkeypatch.setattr(pool, "_now", lambda: 1000.0)

    def _run(connection_id, message):
        request = AttemptRequest(
            model="openai::gpt-x", provider="openai", connection_id=connection_id,
            drive_root=tmp_path, global_limit_usd=1000.0, max_budget_usd=0.01,
        )

        def _boom():
            raise RuntimeError(message)

        with pytest.raises(RuntimeError):
            execute_physical_attempt(request, _boom)

    _run("transient-one", "503 upstream unavailable")
    _run("bad-key", "401 Unauthorized: invalid api key")
    assert pool._COOLDOWN_UNTIL["bad-key"] > pool._COOLDOWN_UNTIL["transient-one"]


def test_a_send_with_no_connection_identity_touches_nothing(tmp_path):
    # The legacy/environment path stamps no identity; it must not poison rotation.
    from ouroboros.usage_accounting import execute_physical_attempt

    request = AttemptRequest(
        model="openai::gpt-x", provider="openai",
        drive_root=tmp_path, global_limit_usd=1000.0, max_budget_usd=0.01,
    )

    def _boom():
        raise RuntimeError("503")

    with pytest.raises(RuntimeError):
        execute_physical_attempt(request, _boom)
    assert pool._COOLDOWN_UNTIL == {}
