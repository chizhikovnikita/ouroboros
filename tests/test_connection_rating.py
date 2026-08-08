"""Per-connection rating: measured from the ledger, honest about what it lacks."""

from __future__ import annotations

import pytest

from ouroboros import connection_pool as pool
from ouroboros import connection_rating as rating
from ouroboros import connections as conns
from ouroboros.usage_accounting import (
    AttemptRequest,
    mark_dispatched,
    mark_unresolved,
    reserve_attempt,
    settle_attempt,
)


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    pool.reset_cooldowns()
    yield
    pool.reset_cooldowns()


def _attempt(tmp_path, connection_id, *, outcome="settled", cost=0.001):
    reservation = reserve_attempt(AttemptRequest(
        model="openai::gpt-x", provider="openai", connection_id=connection_id,
        drive_root=tmp_path, global_limit_usd=1000.0, max_budget_usd=0.01,
    ))
    if outcome == "released":
        from ouroboros.usage_accounting import release_attempt

        release_attempt(reservation)
        return
    mark_dispatched(reservation)
    if outcome == "settled":
        settle_attempt(
            reservation, {"prompt_tokens": 10, "completion_tokens": 10},
            cost_usd=cost, cost_final=True,
        )
    else:
        mark_unresolved(reservation, "provider exploded")


def _run(tmp_path, connection_id, *, ok=0, bad=0, released=0):
    for _ in range(ok):
        _attempt(tmp_path, connection_id, outcome="settled")
    for _ in range(bad):
        _attempt(tmp_path, connection_id, outcome="unresolved")
    for _ in range(released):
        _attempt(tmp_path, connection_id, outcome="released")


# --- honesty about missing data ----------------------------------------------


def test_an_unused_connection_has_no_rate_rather_than_a_zero():
    # A zero would sort it last forever, so it would never get the traffic that
    # would rate it — the pool would collapse onto whatever was tried first.
    stats = rating.ConnectionStats(connection_id="fresh")
    assert stats.success_rate is None
    assert stats.avg_cost_usd is None
    assert stats.rated is False
    assert stats.to_public_dict()["success_rate"] is None


def test_too_few_observations_stay_unrated(tmp_path):
    _run(tmp_path, "thin", ok=rating.MIN_OBSERVATIONS - 1)
    stats = rating.collect_stats(tmp_path)["thin"]
    assert stats.succeeded == rating.MIN_OBSERVATIONS - 1
    assert stats.success_rate is None, "a rate from a couple of sends is noise"


def test_an_unrated_connection_outranks_one_with_real_failures(tmp_path):
    _run(tmp_path, "bad", ok=1, bad=9)
    stats = rating.collect_stats(tmp_path)
    assert rating.rank_value(stats.get("never-used")) > rating.rank_value(stats["bad"])


def test_a_proven_connection_outranks_an_unrated_one(tmp_path):
    _run(tmp_path, "good", ok=20)
    stats = rating.collect_stats(tmp_path)
    assert rating.rank_value(stats["good"]) > rating.rank_value(stats.get("never-used"))


def test_an_unreadable_ledger_leaves_everything_unrated(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr("ouroboros.usage_ledger._read_records_locked", _boom)
    assert rating.collect_stats(tmp_path) == {}


# --- measurement --------------------------------------------------------------


def test_success_and_failure_are_counted_per_connection(tmp_path):
    _run(tmp_path, "a", ok=6, bad=2)
    _run(tmp_path, "b", ok=5)
    stats = rating.collect_stats(tmp_path)
    assert (stats["a"].succeeded, stats["a"].failed) == (6, 2)
    assert stats["a"].success_rate == pytest.approx(6 / 8)
    assert stats["b"].success_rate == 1.0


def test_a_released_reservation_is_not_a_failure(tmp_path):
    # Reserved then handed back without a send is no evidence either way; counting
    # it as a failure would punish a connection for a budget decision.
    _run(tmp_path, "a", ok=5, released=5)
    stats = rating.collect_stats(tmp_path)
    assert stats["a"].released == 5
    assert stats["a"].failed == 0
    assert stats["a"].success_rate == 1.0


def test_realized_cost_is_summed_and_averaged(tmp_path):
    _run(tmp_path, "a", ok=4)
    stats = rating.collect_stats(tmp_path)
    assert stats["a"].cost_usd == pytest.approx(0.004)
    assert stats["a"].avg_cost_usd == pytest.approx(0.001)


def test_attempts_without_an_identity_are_ignored(tmp_path):
    _attempt(tmp_path, "")
    assert rating.collect_stats(tmp_path) == {}


def test_ordering_puts_the_better_connection_first(tmp_path):
    _run(tmp_path, "weak", ok=2, bad=8)
    _run(tmp_path, "strong", ok=10)
    assert rating.order_by_rating(["weak", "strong"], tmp_path)[0] == "strong"


# --- effect on selection -----------------------------------------------------


def _register(tmp_path, connection_id):
    conns.upsert(conns.parse_connection({
        "connection_id": connection_id, "provider": "openai", "kind": "api_key",
    }), tmp_path)
    conns.set_secret(connection_id, {"OPENAI_API_KEY": f"sk-{connection_id}"}, tmp_path)


def test_selection_favours_the_better_rated_connection(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _register(tmp_path, "weak")
    _register(tmp_path, "strong")
    _run(tmp_path, "weak", ok=1, bad=19)
    _run(tmp_path, "strong", ok=20)

    picks = [pool.select("openai::gpt-x", tmp_path).connection_id for _ in range(200)]
    assert picks.count("strong") > picks.count("weak") * 3


def test_selection_still_explores_the_worse_connection(tmp_path, monkeypatch):
    """A pool that never samples its other members cannot notice the favourite rot."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _register(tmp_path, "weak")
    _register(tmp_path, "strong")
    _run(tmp_path, "weak", ok=1, bad=19)
    _run(tmp_path, "strong", ok=20)

    picks = [pool.select("openai::gpt-x", tmp_path).connection_id for _ in range(300)]
    assert picks.count("weak") > 0, "exploitation starved the alternative entirely"


def test_a_newly_added_connection_still_receives_traffic(tmp_path, monkeypatch):
    # The practical consequence of "unrated is not zero": an owner who adds a key
    # must see it used, not shadow-banned until it somehow earns a record.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _register(tmp_path, "established")
    _run(tmp_path, "established", ok=30)
    _register(tmp_path, "brand-new")

    picks = [pool.select("openai::gpt-x", tmp_path).connection_id for _ in range(120)]
    assert picks.count("brand-new") > 0


def test_rating_failure_does_not_break_routing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _register(tmp_path, "a")
    _register(tmp_path, "b")

    def _boom(*args, **kwargs):
        raise RuntimeError("rating exploded")

    monkeypatch.setattr(rating, "collect_stats", _boom)
    assert pool.select("openai::gpt-x", tmp_path) is not None
