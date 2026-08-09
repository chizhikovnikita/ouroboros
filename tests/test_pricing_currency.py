"""Provider-reported cost must reach the ledger in USD, or not at all.

The incident behind this file: polza.ai returns ``cost`` and ``cost_rub`` holding
the same number — the plain field is roubles. Booked as USD it inflated recorded
spend by the whole exchange rate, so a trivial task read as $2.56 instead of a few
cents and the budget fence tripped roughly eighty times too early.
"""

from __future__ import annotations

import pytest

from ouroboros.pricing import normalize_reported_cost


def test_a_plain_cost_with_no_currency_witness_is_left_alone():
    """OpenRouter reports USD under ``cost`` and must keep passing through untouched."""
    usd, note = normalize_reported_cost({"cost": 0.000126})
    assert usd == 0.000126
    assert note == ""


def test_an_explicitly_named_usd_cost_wins():
    """closerouter names the unit (``cost_usd``); that beats every inference."""
    usd, note = normalize_reported_cost({"cost_usd": 3.2e-05, "cost": 999.0})
    assert usd == 3.2e-05
    assert note == "usd_declared"


def test_a_rouble_cost_without_a_configured_rate_becomes_unknown(monkeypatch):
    monkeypatch.delenv("OUROBOROS_RUB_USD_RATE", raising=False)
    usd, note = normalize_reported_cost({"cost": 0.36912818, "cost_rub": 0.36912818})
    # None, never the rouble figure: an unknown cost is a first-class ledger state,
    # a wrong one silently moves the owner's spending limit.
    assert usd is None
    assert note == "unconvertible_RUB"


def test_a_rouble_cost_converts_with_the_owner_rate(monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUB_USD_RATE", "80")
    usd, note = normalize_reported_cost({"cost": 80.0, "cost_rub": 80.0})
    assert usd == pytest.approx(1.0)
    assert note == "converted_RUB"


@pytest.mark.parametrize("rate", ["", "0", "-5", "not-a-number"])
def test_an_unusable_rate_is_the_same_as_no_rate(monkeypatch, rate):
    monkeypatch.setenv("OUROBOROS_RUB_USD_RATE", rate)
    usd, note = normalize_reported_cost({"cost": 10.0, "cost_rub": 10.0})
    assert usd is None
    assert note == "unconvertible_RUB"


def test_a_witness_that_disagrees_proves_nothing(monkeypatch):
    """``cost_rub`` differing from ``cost`` means they are different quantities.

    Guessing which one the plain field is would be exactly the invention this
    normalization exists to prevent, so the plain value passes through as-is.
    """
    monkeypatch.setenv("OUROBOROS_RUB_USD_RATE", "80")
    usd, note = normalize_reported_cost({"cost": 0.5, "cost_rub": 40.0})
    assert usd == 0.5
    assert note == ""


def test_a_missing_cost_stays_missing():
    assert normalize_reported_cost({}) == (None, "")
    assert normalize_reported_cost({"cost": None, "cost_rub": 1.0}) == (None, "")


def test_a_non_numeric_cost_is_not_coerced():
    assert normalize_reported_cost({"cost": "0.37", "cost_rub": "0.37"}) == (None, "")
