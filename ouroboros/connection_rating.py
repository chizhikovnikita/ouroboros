"""Per-connection rating, measured from the usage ledger.

Stage 3 of the pool. Every physical send already lands in
``state/usage_attempts.jsonl`` with its model, provider, terminal state, realized
cost and — since stage 2 — the connection that carried it. So the measured half of
the rating needs no new telemetry stream: it is a fold over the money ledger, which
is also the only authority that cannot quietly disagree with the bill.

The rule that shapes everything here: **absence of data is None, never zero.**
A connection nobody has used yet has no success rate — not a success rate of 0. If
it scored zero it would sort last forever and never get the traffic that would rate
it, so the pool would collapse onto whichever connection happened to be tried first
and would stop noticing the others degrade. Unrated connections are therefore
ranked optimistically and keep an exploration share of traffic.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

log = logging.getLogger(__name__)


# A rate computed from one or two sends is noise. Below this the connection counts
# as unrated: it keeps its optimistic standing rather than being condemned or
# crowned by a single outcome.
MIN_OBSERVATIONS = 5

# What an unrated connection is assumed to be worth while it earns its own numbers.
# Deliberately just under a perfect record: a proven-good connection outranks an
# unknown one, but an unknown one still outranks anything with real failures.
OPTIMISTIC_SUCCESS_RATE = 0.95


@dataclass(frozen=True)
class ConnectionStats:
    """Measured behavior of one connection. Unknown fields are None, never 0."""

    connection_id: str
    attempts: int = 0
    succeeded: int = 0
    failed: int = 0
    released: int = 0
    cost_usd: float = 0.0
    cost_known_attempts: int = 0

    @property
    def observations(self) -> int:
        """Attempts that actually reached a provider and produced an outcome."""
        return self.succeeded + self.failed

    @property
    def success_rate(self) -> Optional[float]:
        if self.observations < MIN_OBSERVATIONS:
            return None
        return self.succeeded / self.observations

    @property
    def avg_cost_usd(self) -> Optional[float]:
        if self.cost_known_attempts <= 0:
            return None
        return self.cost_usd / self.cost_known_attempts

    @property
    def rated(self) -> bool:
        return self.success_rate is not None

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "attempts": self.attempts,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "released": self.released,
            "observations": self.observations,
            # None travels to the UI as null on purpose: "not measured yet" is a
            # different statement from "measured, and it is zero".
            "success_rate": self.success_rate,
            "avg_cost_usd": self.avg_cost_usd,
            "cost_usd": self.cost_usd if self.cost_known_attempts else None,
            "rated": self.rated,
        }


def _fold_attempts(records: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Latest state per attempt, keeping the connection id whichever row carried it."""
    folded: Dict[str, Dict[str, Any]] = {}
    for row in records:
        attempt_id = str(row.get("attempt_id") or "")
        if not attempt_id:
            continue
        current = folded.setdefault(attempt_id, {"connection_id": "", "state": "", "cost_usd": None})
        connection_id = str(row.get("connection_id") or "")
        if connection_id:
            current["connection_id"] = connection_id
        state = str(row.get("state") or "")
        if state:
            current["state"] = state
        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)):
            current["cost_usd"] = float(cost)
    return folded


def collect_stats(root: pathlib.Path | str | None = None) -> Dict[str, ConnectionStats]:
    """Fold the ledger into per-connection statistics.

    An unreadable ledger yields an EMPTY map: not knowing how connections have
    behaved must degrade routing to "treat everything as unrated", never to a
    refusal to route.
    """
    try:
        from ouroboros.usage_ledger import _drive_root, _locked, _read_records_locked

        drive_root = _drive_root(root)
        with _locked(drive_root):
            records = _read_records_locked(drive_root)
    except Exception:
        log.debug("connection stats unavailable", exc_info=True)
        return {}

    counters: Dict[str, Dict[str, float]] = {}
    for attempt in _fold_attempts(records).values():
        connection_id = attempt["connection_id"]
        if not connection_id:
            continue
        bucket = counters.setdefault(
            connection_id,
            {"attempts": 0, "succeeded": 0, "failed": 0, "released": 0, "cost_usd": 0.0, "cost_known": 0},
        )
        bucket["attempts"] += 1
        state = attempt["state"]
        if state == "settled":
            bucket["succeeded"] += 1
        elif state == "unresolved":
            bucket["failed"] += 1
        elif state == "released":
            # Reserved then given back without a send: no evidence either way, so
            # it is tracked but never counted as a failure.
            bucket["released"] += 1
        cost = attempt["cost_usd"]
        if cost is not None:
            bucket["cost_usd"] += cost
            bucket["cost_known"] += 1

    return {
        connection_id: ConnectionStats(
            connection_id=connection_id,
            attempts=int(bucket["attempts"]),
            succeeded=int(bucket["succeeded"]),
            failed=int(bucket["failed"]),
            released=int(bucket["released"]),
            cost_usd=float(bucket["cost_usd"]),
            cost_known_attempts=int(bucket["cost_known"]),
        )
        for connection_id, bucket in counters.items()
    }


def rank_value(stats: Optional[ConnectionStats]) -> float:
    """Sortable standing for one connection. Higher is better.

    An unrated connection scores just under a perfect record: better than anything
    with real failures, worse than a connection that has proven itself — and, most
    importantly, high enough to actually receive traffic and become rated.
    """
    if stats is None or not stats.rated:
        return OPTIMISTIC_SUCCESS_RATE
    return float(stats.success_rate or 0.0)


def order_by_rating(
    connection_ids: List[str],
    root: pathlib.Path | str | None = None,
    *,
    stats: Optional[Mapping[str, ConnectionStats]] = None,
) -> List[str]:
    """Best-first ordering. Ties keep the caller's order for the caller to break."""
    table = dict(stats) if stats is not None else collect_stats(root)
    return sorted(connection_ids, key=lambda cid: -rank_value(table.get(cid)))
