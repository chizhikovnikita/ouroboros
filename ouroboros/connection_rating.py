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


OUTCOMES_REL = pathlib.Path("state/connection_outcomes.jsonl")

# Task-outcome quality is the noisiest signal here: one task's verdict says much
# more about the task than about the key that carried it. It therefore enters with
# a small weight and only once a connection has enough verdicts to mean anything.
QUALITY_WEIGHT = 0.25
MIN_QUALITY_OBSERVATIONS = 10


def record_task_outcome(
    root_task_id: str,
    accepted: bool,
    root: pathlib.Path | str | None = None,
) -> None:
    """Record one task's acceptance verdict, keyed by root task.

    Written WHERE THE VERDICT HAPPENS rather than reconstructed later: the join to
    connections is done at read time through the ledger's own ``root_task_id``, so
    nothing here has to guess which connection served which turn.

    Best-effort: a rating signal must never be able to fail a completed task.
    """
    root_task_id = str(root_task_id or "").strip()
    if not root_task_id:
        return
    try:
        from ouroboros.usage_ledger import _drive_root
        from ouroboros.utils import append_jsonl

        path = _drive_root(root) / OUTCOMES_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(path, {"root_task_id": root_task_id, "accepted": bool(accepted)})
    except Exception:
        log.debug("connection outcome not recorded", exc_info=True)


def _task_verdicts(root: pathlib.Path | str | None) -> Dict[str, bool]:
    """Latest verdict per root task; an unreadable file simply yields none."""
    try:
        from ouroboros.usage_ledger import _drive_root

        path = _drive_root(root) / OUTCOMES_REL
        if not path.is_file():
            return {}
        import json

        verdicts: Dict[str, bool] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            task_id = str(row.get("root_task_id") or "")
            if task_id:
                verdicts[task_id] = bool(row.get("accepted"))
        return verdicts
    except Exception:
        log.debug("task verdicts unavailable", exc_info=True)
        return {}


def quality_by_connection(root: pathlib.Path | str | None = None) -> Dict[str, Optional[float]]:
    """Share of accepted tasks per connection, or None where there is too little.

    A connection that served both an accepted and a rejected task is credited for
    both — attribution here is honestly coarse, which is exactly why the weight is
    small and the floor is high.
    """
    verdicts = _task_verdicts(root)
    if not verdicts:
        return {}
    try:
        from ouroboros.usage_ledger import _drive_root, _locked, _read_records_locked

        drive_root = _drive_root(root)
        with _locked(drive_root):
            records = _read_records_locked(drive_root)
    except Exception:
        return {}

    seen: Dict[str, Dict[str, int]] = {}
    counted: set[tuple[str, str]] = set()
    for row in records:
        connection_id = str(row.get("connection_id") or "")
        task_id = str(row.get("root_task_id") or "")
        if not connection_id or task_id not in verdicts:
            continue
        # One task counts ONCE per connection however many sends it took, or a
        # chatty task would drown out every other verdict.
        if (connection_id, task_id) in counted:
            continue
        counted.add((connection_id, task_id))
        bucket = seen.setdefault(connection_id, {"accepted": 0, "total": 0})
        bucket["total"] += 1
        if verdicts[task_id]:
            bucket["accepted"] += 1

    return {
        connection_id: (
            bucket["accepted"] / bucket["total"]
            if bucket["total"] >= MIN_QUALITY_OBSERVATIONS
            else None
        )
        for connection_id, bucket in seen.items()
    }


def rank_value(
    stats: Optional[ConnectionStats],
    *,
    quality: Optional[float] = None,
) -> float:
    """Sortable standing for one connection. Higher is better.

    An unrated connection scores just under a perfect record: better than anything
    with real failures, worse than a connection that has proven itself — and, most
    importantly, high enough to actually receive traffic and become rated.

    Task-outcome quality only adjusts an ALREADY-measured connection: letting a
    noisy verdict share move an unrated one would defeat the optimistic start that
    keeps new connections in rotation.
    """
    if stats is None or not stats.rated:
        return OPTIMISTIC_SUCCESS_RATE
    base = float(stats.success_rate or 0.0)
    if quality is None:
        return base
    return base * (1.0 - QUALITY_WEIGHT) + float(quality) * QUALITY_WEIGHT


def order_by_rating(
    connection_ids: List[str],
    root: pathlib.Path | str | None = None,
    *,
    stats: Optional[Mapping[str, ConnectionStats]] = None,
) -> List[str]:
    """Best-first ordering. Ties keep the caller's order for the caller to break."""
    table = dict(stats) if stats is not None else collect_stats(root)
    quality = quality_by_connection(root)
    return sorted(
        connection_ids,
        key=lambda cid: -rank_value(table.get(cid), quality=quality.get(cid)),
    )


def record_outcome_from_loop(
    task: Mapping[str, Any],
    loop_outcome: Mapping[str, Any],
    root: pathlib.Path | str | None = None,
) -> None:
    """Derive one task-quality verdict from a finished loop outcome and record it.

    Lives here rather than in the task pipeline so the rules about what counts as
    a good outcome stay next to the code that consumes them — and so the pipeline
    keeps one line instead of a policy.

    Best-effort: a rating signal must never fail a task that already completed.
    """
    try:
        from ouroboros.outcomes import ACCEPTANCE_ACCEPTED

        root_task_id = str(task.get("root_task_id") or task.get("id") or "")
        if not root_task_id:
            return

        decision = loop_outcome.get("acceptance_decision")
        if isinstance(decision, dict) and decision.get("status"):
            accepted = str(decision.get("status")) == ACCEPTANCE_ACCEPTED
        else:
            # No acceptance panel ran. Execution health is a weaker but honest
            # statement about the send path; anything unknown records nothing at
            # all rather than guessing.
            axes = loop_outcome.get("outcome_axes") or {}
            execution = axes.get("execution") if isinstance(axes, dict) else None
            status = str((execution or {}).get("status") or "") if isinstance(execution, dict) else ""
            if not status:
                return
            accepted = status in ("ok", "completed", "success")

        record_task_outcome(root_task_id, accepted, root)
    except Exception:
        log.debug("outcome verdict not derived", exc_info=True)
