"""The data-sensitivity gate: sensitive work never reaches a training provider.

Every test here drives the REAL selection path. Monkeypatching the resolver would
prove the mechanics and miss the fence — this repo has already shipped a
protected-path bypass that way once.
"""

from __future__ import annotations

import pytest

from ouroboros import connection_pool as pool
from ouroboros import connections as conns
from ouroboros.usage_accounting import UsageScope, current_usage_scope, usage_scope


SENSITIVE = conns.SENSITIVITY_SENSITIVE


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pool.reset_cooldowns()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OUROBOROS_DISABLE_CONNECTION_POOL", raising=False)
    yield
    pool.reset_cooldowns()


def _add(tmp_path, connection_id, privacy):
    conns.upsert(conns.parse_connection({
        "connection_id": connection_id, "provider": "openai", "kind": "api_key", "privacy": privacy,
    }), tmp_path)
    conns.set_secret(connection_id, {"OPENAI_API_KEY": f"sk-{connection_id}"}, tmp_path)


# --- the gate itself ---------------------------------------------------------


def test_sensitive_work_never_selects_a_training_connection(tmp_path):
    _add(tmp_path, "clean", conns.PRIVACY_NO_TRAINING)
    _add(tmp_path, "trains", conns.PRIVACY_TRAINS_ON_DATA)
    picks = {
        pool.select("openai::gpt-x", tmp_path, sensitivity=SENSITIVE).connection_id
        for _ in range(80)
    }
    assert picks == {"clean"}


def test_unknown_privacy_is_not_permission(tmp_path):
    # Whether a vendor trains on submitted data is a contractual fact; an
    # undeclared connection is exactly the one we cannot vouch for.
    _add(tmp_path, "undeclared", conns.PRIVACY_UNKNOWN)
    with pytest.raises(pool.NoPermittedConnection):
        pool.select("openai::gpt-x", tmp_path, sensitivity=SENSITIVE)


def test_public_work_may_use_any_connection(tmp_path):
    _add(tmp_path, "trains", conns.PRIVACY_TRAINS_ON_DATA)
    assert pool.select("openai::gpt-x", tmp_path, sensitivity="public").connection_id == "trains"


def test_a_cheap_fast_well_rated_training_connection_is_still_excluded(tmp_path):
    # The gate is not a ranking penalty a good score can outweigh.
    from ouroboros.connection_rating import collect_stats
    from ouroboros.usage_accounting import (
        AttemptRequest, mark_dispatched, reserve_attempt, settle_attempt,
    )

    _add(tmp_path, "clean", conns.PRIVACY_NO_TRAINING)
    _add(tmp_path, "trains", conns.PRIVACY_TRAINS_ON_DATA)
    for _ in range(30):
        reservation = reserve_attempt(AttemptRequest(
            model="openai::gpt-x", provider="openai", connection_id="trains",
            drive_root=tmp_path, global_limit_usd=1000.0, max_budget_usd=0.01,
        ))
        mark_dispatched(reservation)
        settle_attempt(reservation, {"prompt_tokens": 1, "completion_tokens": 1}, cost_usd=0.0, cost_final=True)
    assert collect_stats(tmp_path)["trains"].success_rate == 1.0

    picks = {
        pool.select("openai::gpt-x", tmp_path, sensitivity=SENSITIVE).connection_id
        for _ in range(60)
    }
    assert picks == {"clean"}


# --- fail-closed, not fail-open ----------------------------------------------


def test_no_cleared_connection_refuses_rather_than_falling_back_to_the_environment(
    tmp_path, monkeypatch
):
    """The most important test here.

    Returning None would mean "pool has no opinion", and the caller would then use
    whatever key is in the environment — which carries no privacy declaration at
    all. That is the exact send this gate exists to prevent.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment-key")
    _add(tmp_path, "trains", conns.PRIVACY_TRAINS_ON_DATA)
    with pytest.raises(pool.NoPermittedConnection):
        pool.select("openai::gpt-x", tmp_path, sensitivity=SENSITIVE)


def test_the_overlay_refuses_instead_of_leaving_the_environment_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment-key")
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path, raising=False)
    _add(tmp_path, "trains", conns.PRIVACY_TRAINS_ON_DATA)
    target = {"provider": "openai", "api_key": "sk-environment-key"}
    with usage_scope(UsageScope(data_sensitivity=SENSITIVE)):
        with pytest.raises(pool.NoPermittedConnection):
            pool.apply_to_target(dict(target), "openai::gpt-x", tmp_path)


def test_an_empty_registry_refuses_sensitive_work(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment-key")
    with pytest.raises(pool.NoPermittedConnection):
        pool.select("openai::gpt-x", tmp_path, sensitivity=SENSITIVE)


def test_disabling_the_pool_is_not_a_privacy_bypass(tmp_path, monkeypatch):
    # An availability switch must not double as a way to turn the gate off.
    monkeypatch.setenv("OUROBOROS_DISABLE_CONNECTION_POOL", "1")
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path, raising=False)
    with usage_scope(UsageScope(data_sensitivity=SENSITIVE)):
        with pytest.raises(pool.NoPermittedConnection):
            pool.apply_to_target({"provider": "openai", "api_key": "sk-env"}, "openai::gpt-x", tmp_path)


def test_an_unreadable_sensitivity_is_treated_as_sensitive(monkeypatch):
    def _boom():
        raise RuntimeError("scope unavailable")

    monkeypatch.setattr("ouroboros.usage_accounting.current_usage_scope", _boom)
    assert pool.active_sensitivity() == SENSITIVE


def test_an_unknown_label_is_treated_as_the_strictest(tmp_path):
    # A typo or a label from a newer version must not silently downgrade.
    _add(tmp_path, "trains", conns.PRIVACY_TRAINS_ON_DATA)
    with pytest.raises(pool.NoPermittedConnection):
        pool.select("openai::gpt-x", tmp_path, sensitivity="ultra-secret-tier")


# --- inheritance: a child cannot widen its parent -----------------------------


def test_a_nested_scope_cannot_weaken_its_parent():
    with usage_scope(UsageScope(data_sensitivity=SENSITIVE)):
        with usage_scope(UsageScope(data_sensitivity="public")):
            assert current_usage_scope().data_sensitivity == SENSITIVE


def test_a_nested_scope_may_tighten():
    with usage_scope(UsageScope(data_sensitivity="public")):
        with usage_scope(UsageScope(data_sensitivity=SENSITIVE)):
            assert current_usage_scope().data_sensitivity == SENSITIVE


def test_a_child_that_declares_nothing_inherits_the_parent_label():
    with usage_scope(UsageScope(data_sensitivity=SENSITIVE)):
        with usage_scope(UsageScope(task_id="child")):
            assert current_usage_scope().data_sensitivity == SENSITIVE


def test_the_parent_label_is_restored_after_the_child_exits():
    with usage_scope(UsageScope(data_sensitivity="public")):
        with usage_scope(UsageScope(data_sensitivity=SENSITIVE)):
            pass
        assert current_usage_scope().data_sensitivity == "public"


def test_the_ambient_scope_drives_selection_without_an_explicit_argument(tmp_path):
    _add(tmp_path, "clean", conns.PRIVACY_NO_TRAINING)
    _add(tmp_path, "trains", conns.PRIVACY_TRAINS_ON_DATA)
    with usage_scope(UsageScope(data_sensitivity=SENSITIVE)):
        picks = {pool.select("openai::gpt-x", tmp_path).connection_id for _ in range(60)}
    assert picks == {"clean"}


# --- task-level default ------------------------------------------------------


def test_self_modification_defaults_to_sensitive(monkeypatch):
    from ouroboros.agent import _task_data_sensitivity

    monkeypatch.delenv("OUROBOROS_DEFAULT_DATA_SENSITIVITY", raising=False)
    assert _task_data_sensitivity({"type": "evolution"}) == SENSITIVE
    assert _task_data_sensitivity({"type": "task"}) == ""


def test_an_explicit_task_label_wins_over_the_default(monkeypatch):
    from ouroboros.agent import _task_data_sensitivity

    monkeypatch.setenv("OUROBOROS_DEFAULT_DATA_SENSITIVITY", "public")
    assert _task_data_sensitivity({"task_constraint": {"data_sensitivity": SENSITIVE}}) == SENSITIVE


def test_the_owner_can_make_everything_sensitive_by_default(monkeypatch):
    from ouroboros.agent import _task_data_sensitivity

    monkeypatch.setenv("OUROBOROS_DEFAULT_DATA_SENSITIVITY", SENSITIVE)
    assert _task_data_sensitivity({"type": "task"}) == SENSITIVE


def test_a_subagent_constraint_keeps_the_label(monkeypatch):
    """Every normalizer branch must carry it — the restricted branches build a
    fresh constraint, and dropping it there would let a child widen its parent."""
    from ouroboros.contracts.task_constraint import normalize_task_constraint

    for mode in ("normal", "local_readonly_subagent", "acting_subagent"):
        constraint = normalize_task_constraint({"mode": mode, "data_sensitivity": SENSITIVE})
        assert constraint.data_sensitivity == SENSITIVE, f"{mode} dropped the label"
