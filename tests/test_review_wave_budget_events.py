"""Both review-wave budget verdicts must land as typed rows, not be dropped.

The incident: `review_wave_budget_partial_unknown` is emitted only so that an
admitted wave whose estimate contained an unknowable slot price stays visible.
It had no registered handler, so the supervisor dropped it with
"No handler for worker event type ... — event dropped" and the gap it reports
became indistinguishable from an absence of events.
"""

from __future__ import annotations

import json

import pytest

from supervisor.events import EVENT_HANDLERS


BUDGET_EVENT_TYPES = (
    "review_wave_budget_insufficient",
    "review_wave_budget_partial_unknown",
)


@pytest.mark.parametrize("event_type", BUDGET_EVENT_TYPES)
def test_every_review_wave_budget_verdict_has_a_handler(event_type):
    assert event_type in EVENT_HANDLERS


@pytest.mark.parametrize("event_type", BUDGET_EVENT_TYPES)
def test_the_verdict_is_persisted_as_a_typed_events_row(tmp_path, event_type):
    class _Ctx:
        DRIVE_ROOT = tmp_path

    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    EVENT_HANDLERS[event_type](
        {
            "type": event_type,
            "ts": "2026-08-09T00:00:00+00:00",
            "surface": "task_acceptance",
            "unpriced_slots": 3,
            "estimated_wave_usd": None,
        },
        _Ctx(),
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["type"] == event_type
    # The unknown count is the whole reason the event exists — it must survive.
    assert rows[0]["unpriced_slots"] == 3
    assert rows[0]["ts"] == "2026-08-09T00:00:00+00:00"


def test_both_verdicts_share_one_handler():
    """One concept, one carrier: two admission outcomes of the same budget gate."""
    handlers = {EVENT_HANDLERS[name] for name in BUDGET_EVENT_TYPES}
    assert len(handlers) == 1
