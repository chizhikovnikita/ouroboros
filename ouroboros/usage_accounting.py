"""Durable physical-model-attempt accounting.

The append-only JSONL ledger is the monetary authority.  Existing
``llm_usage`` events and ``state.json`` remain compatibility projections and
carry ledger attempt ids, so they can never become a second charge source.

The implementation is deliberately small: no hash chain, fanout reservation,
epoch/reconcile platform, or per-attempt snapshot database.  A projection is
replayed from validated records under the same short cross-process lock used
for budget check + append + fsync; network I/O always happens outside that lock.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import pathlib
import threading
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple

from ouroboros.pricing import estimate_cost_optional
# One-way seam: the durable ledger substrate (locking, atomic append + fsync, row
# and transition validation, torn-tail quarantine) lives in ouroboros/usage_ledger.py
# and knows nothing about reservations, budgets, pricing, or projections. This module
# is the ACCOUNTING POLICY on top of it and imports FROM it; usage_ledger must never
# import back. The private names are re-bound here so existing call sites — and the
# tests that monkeypatch `usage_accounting._locked` — keep working unchanged.
from ouroboros.usage_ledger import (  # noqa: F401 — re-exported substrate
    LEDGER_REL,
    QUARANTINE_REL,
    UsageAccountingError,
    UsageLedgerCorrupt,
    _append_bytes_fsync,
    _append_rows_locked,
    _drive_root,
    _final_rows,
    _locked,
    _named_lock,
    _number,
    _read_records_locked,
    # Re-exported although THIS module no longer reads it: the terminal-state set is
    # part of the substrate's vocabulary, and a policy function that terminalizes an
    # attempt needs to name those states. Dropping it from the re-export would make the
    # split look clean here and break a caller that arrives from another branch.
    _TERMINAL,
    _validate_records,
    _write_bytes_atomic_fsync,
)
from ouroboros.utils import append_jsonl, atomic_write_json, utc_now_iso

log = logging.getLogger(__name__)

IMPORT_REL = pathlib.Path("state/usage_import_watermark.json")

__all__ = (
    "AttemptRequest", "AttemptReservation", "BudgetExceeded", "PhysicalAttemptLimitExceeded",
    "UsageAccountingError", "UsageLedgerCorrupt", "UsageScope", "capture_attempt_ids",
    "current_usage_scope",
    "ensure_legacy_imported", "execute_physical_attempt", "execute_physical_attempt_async",
    "mark_dispatched", "mark_unresolved", "physical_attempt_limit",
    "record_subscription_session",
    "record_unmetered_external_dispatch", "release_attempt", "reserve_attempt", "settle_attempt",
    "usage_breakdown", "usage_from_response", "usage_projection", "usage_scope",
    "review_wave_admission",
)
_CURRENT_SCOPE: contextvars.ContextVar[Optional["UsageScope"]] = contextvars.ContextVar(
    "ouroboros_usage_scope", default=None
)
_ATTEMPT_COLLECTOR: contextvars.ContextVar[Optional[list[str]]] = contextvars.ContextVar(
    "ouroboros_usage_attempt_collector", default=None
)
_PHYSICAL_LIMIT: contextvars.ContextVar[Optional["_AttemptLimit"]] = contextvars.ContextVar(
    "ouroboros_physical_attempt_limit", default=None
)


class BudgetExceeded(UsageAccountingError):
    """Raised before dispatch when a known budget would be exceeded."""

    def __init__(self, message: str, *, limit_scope: str = "global", root_task_id: str = "") -> None:
        super().__init__(message)
        self.limit_scope = str(limit_scope or "global")
        self.root_task_id = str(root_task_id or "")


class PhysicalAttemptLimitExceeded(UsageAccountingError):
    """Raised before a provider send would exceed the caller's actor-local rail."""


@dataclass
class _AttemptLimit:
    maximum: int
    used: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class UsageScope:
    drive_root: pathlib.Path | str | None = None
    task_id: str = ""
    root_task_id: str = ""
    parent_task_id: str = ""
    category: str = "task"
    source: str = "llm"
    global_limit_usd: Optional[float] = None
    root_limit_usd: Optional[float] = None


@dataclass(frozen=True)
class AttemptRequest:
    model: str
    provider: str
    prompt_tokens_estimate: int = 0
    max_completion_tokens: int = 0
    reservation_usd: Optional[float] = None
    max_budget_usd: Optional[float] = None
    global_limit_usd: Optional[float] = None
    drive_root: pathlib.Path | str | None = None
    task_id: str = ""
    root_task_id: str = ""
    parent_task_id: str = ""
    category: str = ""
    source: str = ""
    root_limit_usd: Optional[float] = None
    force_unknown_reservation: bool = False
    # WHICH pooled connection carried this send. Captured where the connection is
    # chosen and passed by value, never re-derived later from the model string:
    # two connections to the same provider are indistinguishable by model alone,
    # and per-connection rating and in-flight counting both key off this.
    connection_id: str = ""


@dataclass(frozen=True)
class AttemptReservation:
    attempt_id: str
    drive_root: pathlib.Path
    model: str
    provider: str
    reservation_upper_bound_usd: Optional[float]
    connection_id: str = ""


@contextlib.contextmanager
def usage_scope(scope: UsageScope) -> Iterator[UsageScope]:
    """Bind task/root attribution for physical sends in this execution context."""
    token = _CURRENT_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _CURRENT_SCOPE.reset(token)


def current_usage_scope() -> Optional[UsageScope]:
    """Return the immutable scope bound to this execution context, if any."""
    return _CURRENT_SCOPE.get()


@contextlib.contextmanager
def capture_attempt_ids() -> Iterator[list[str]]:
    """Collect physical attempt ids for one compatibility ``llm_usage`` row."""
    bucket: list[str] = []
    token = _ATTEMPT_COLLECTOR.set(bucket)
    try:
        yield bucket
    finally:
        _ATTEMPT_COLLECTOR.reset(token)


@contextlib.contextmanager
def physical_attempt_limit(maximum: int) -> Iterator[None]:
    """Bound physical provider sends in this actor context (acceptance uses 2)."""
    state = _AttemptLimit(maximum=max(0, int(maximum)))
    token = _PHYSICAL_LIMIT.set(state)
    try:
        yield
    finally:
        _PHYSICAL_LIMIT.reset(token)


def _claim_physical_dispatch() -> None:
    state = _PHYSICAL_LIMIT.get()
    if state is None:
        return
    with state.lock:
        if state.used >= state.maximum:
            raise PhysicalAttemptLimitExceeded(f"physical attempt limit exhausted ({state.used}/{state.maximum})")
        state.used += 1


def _merge_scope(request: AttemptRequest) -> Tuple[AttemptRequest, UsageScope]:
    bound = _CURRENT_SCOPE.get() or UsageScope()
    scope = UsageScope(
        drive_root=request.drive_root or bound.drive_root,
        task_id=str(request.task_id or bound.task_id or ""),
        root_task_id=str(request.root_task_id or bound.root_task_id or ""),
        parent_task_id=str(request.parent_task_id or bound.parent_task_id or ""),
        category=str(request.category or bound.category or "task"),
        source=str(request.source or bound.source or "llm"),
        global_limit_usd=(
            request.global_limit_usd if request.global_limit_usd is not None else bound.global_limit_usd
        ),
        root_limit_usd=(request.root_limit_usd if request.root_limit_usd is not None else bound.root_limit_usd),
    )
    if not scope.root_task_id and scope.task_id:
        scope = replace(scope, root_task_id=scope.task_id)
    if request.global_limit_usd is None and scope.global_limit_usd is not None:
        request = replace(request, global_limit_usd=scope.global_limit_usd)
    return request, scope


def _summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    settled = confirmed = estimated = reserved = unresolved = 0.0
    unknown = 0
    # Finality is a COUNT of OPEN ROWS, not a truthiness test on dollar sums. Three of the
    # four old terms asked a STATE question of a float, so any row that is genuinely open
    # while holding $0.00 disappeared from the predicate entirely:
    #   * an ESTIMATED $0.00 — what the engine reports for a delegated subscription run
    #     whose cash it has not settled (all 8 estimated rows on a live 60-run page); and
    #   * a DISPATCHED row whose reservation is exactly 0.0, which `_reservation_cost`
    #     returns for `provider="local"`, so the projection claimed `cost_final: True`
    #     with a physical send still in flight.
    # A row is final when it is SETTLED at a known price its writer called final; anything
    # else is open, however little it costs. `_final_rows` keys by attempt_id, so a settled
    # row REPLACES its own reserved/dispatched predecessor — a row still open here is
    # really still open, and a released reservation is in neither branch.
    #
    # The count is also the DISCLOSED CAUSE, returned as `non_final_rows`: a projection
    # reporting `cost_final: false` with every dollar bucket at zero and `unknown` at zero
    # is a flag no reader can reconstruct.
    non_final_rows = 0
    counts: Dict[str, int] = {}
    # Separate "sessions and quota" axis: subscription work is already paid for, so
    # it contributes exactly $0 to money, and its real scarce resource (sessions and
    # the window that grants them) is counted here instead of being faked as cash.
    sessions = 0
    session_windows: Dict[str, str] = {}
    for row in rows:
        state = str(row.get("state") or "")
        if str(row.get("kind") or "") == "subscription_session":
            sessions += 1
            route = str(row.get("subscription_route") or "")
            reset_at = str(row.get("subscription_reset_at") or "")
            if route and reset_at:
                session_windows[route] = max(session_windows.get(route, ""), reset_at)
        if str(row.get("kind") or "") == "legacy_metadata":
            ambiguous = max(1, int(row.get("ambiguous_call_count") or 1))
            counts["metadata_only"] = counts.get("metadata_only", 0) + ambiguous
            continue
        counts[state] = counts.get(state, 0) + 1
        pricing_unknown = row.get("pricing_known") is False
        if state == "settled":
            cost = _number(row.get("cost_usd"))
            if cost is None:
                unknown += 1
                non_final_rows += 1
                bound = _number(row.get("reservation_upper_bound_usd"))
                if bound is not None:
                    unresolved += bound
            else:
                settled += cost
                if bool(row.get("cost_final")):
                    confirmed += cost
                else:
                    estimated += cost
                    non_final_rows += 1
        elif state == "reserved":
            non_final_rows += 1
            bound = _number(row.get("reservation_upper_bound_usd"))
            if bound is None or pricing_unknown:
                unknown += 1
            if bound is not None:
                reserved += bound
        elif state in {"dispatched", "unresolved"}:
            non_final_rows += 1
            bound = _number(row.get("reservation_upper_bound_usd"))
            if bound is None or pricing_unknown:
                unknown += 1
            if bound is not None:
                unresolved += bound
    settled, confirmed, estimated, reserved, unresolved = (
        round(value, 6) for value in (settled, confirmed, estimated, reserved, unresolved)
    )
    return {
        "settled_usd": settled,
        "confirmed_usd": confirmed,
        "estimated_usd": estimated,
        "reserved_usd": reserved,
        "unresolved_upper_bound_usd": unresolved,
        "accounted_usd": round(settled + reserved + unresolved, 6),
        "unknown_unmetered": unknown,
        # Every row that increments `unknown` is open, so the old `not unknown` term is
        # subsumed here rather than dropped.
        "non_final_rows": non_final_rows,
        "cost_final": not non_final_rows,
        "attempt_counts": counts,
        "subscription_sessions": sessions,
        "subscription_windows": session_windows,
    }


def _with_limit(summary: Dict[str, Any], limit: Optional[float]) -> Dict[str, Any]:
    if limit is None:
        return summary
    summary["limit_usd"] = round(max(0.0, float(limit)), 6)
    summary["remaining_known_usd"] = round(max(0.0, summary["limit_usd"] - float(summary["accounted_usd"])), 6)
    return summary


def _with_integrity(summary: Dict[str, Any], degraded: bool) -> Dict[str, Any]:
    """Attach ledger integrity and prevent a torn tail from claiming final cost."""
    summary["integrity_degraded"] = bool(degraded)
    if degraded:
        summary["cost_final"] = False
    return summary


def usage_projection(
    drive_root: pathlib.Path | str | None = None,
    *,
    root_task_id: str = "",
    global_limit_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """Return a replayed global projection, or one root/subtree projection."""
    root = _drive_root(drive_root)
    with _locked(root):
        final = list(_final_rows(_read_records_locked(root)).values())
    integrity_degraded = (root / QUARANTINE_REL).is_file()
    if root_task_id:
        final = [row for row in final if str(row.get("root_task_id") or "") == root_task_id]
        limits = [_number(row.get("root_limit_usd")) for row in final]
        known_limits = [value for value in limits if value is not None]
        result = _with_limit(_summary(final), min(known_limits) if known_limits else None)
        return _with_integrity(result, integrity_degraded)
    if global_limit_usd is not None:
        configured_limit = max(0.0, float(global_limit_usd))
    else:
        try:
            configured_limit = float(os.environ.get("TOTAL_BUDGET", "10") or 0.0)
        except (TypeError, ValueError):
            configured_limit = 10.0
    result = (
        _with_limit(_summary(final), configured_limit)
        if global_limit_usd is not None or configured_limit > 0
        else _summary(final)
    )
    root_ids = sorted({str(row.get("root_task_id") or "") for row in final if row.get("root_task_id")})
    result["by_root"] = {}
    for rid in root_ids:
        root_rows = [row for row in final if str(row.get("root_task_id") or "") == rid]
        known_limits = [
            value for value in (_number(row.get("root_limit_usd")) for row in root_rows) if value is not None
        ]
        result["by_root"][rid] = _with_integrity(
            _with_limit(_summary(root_rows), min(known_limits) if known_limits else None),
            integrity_degraded,
        )
    return _with_integrity(result, integrity_degraded)


def _physical_call_count(row: Dict[str, Any]) -> int:
    kind = str(row.get("kind") or "attempt")
    if kind == "legacy_metadata":
        return 0
    if kind == "legacy_delta":
        return 0
    # A subscription session is not a core-mediated physical provider send; it is
    # counted on the sessions axis instead (see record_subscription_session).
    if kind == "subscription_session":
        return 0
    return 1 if str(row.get("state") or "") in {"dispatched", "settled", "unresolved"} else 0


def _breakdown_bucket(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    bucket = _summary(rows)

    def summed(field: str) -> Optional[int]:
        """Sum the rows that HAVE this count; None when not one of them does.

        `int(row.get(field) or 0)` collapsed an ABSENT count into a reported zero, so a
        bucket whose provider returned no token counts at all published a confident
        "0 tokens" — the render-unknown-as-zero shape this module refuses everywhere
        else (`cost = None  # legacy zero may mean unknown pricing, never "free"`). A
        zero here is now only ever a MEASURED zero, and a partially-reporting bucket
        still sums the rows that did report rather than being erased by the ones that
        did not."""
        present = [row.get(field) for row in rows if row.get(field) is not None]
        return sum(max(0, int(value)) for value in present) if present else None

    prompt = summed("prompt_tokens")
    completion = summed("completion_tokens")
    prompt_cache_ttls: Dict[str, int] = {}
    for row in rows:
        ttl = str(row.get("prompt_cache_ttl") or "").strip()
        if ttl:
            prompt_cache_ttls[ttl] = prompt_cache_ttls.get(ttl, 0) + _physical_call_count(row)
    bucket.update({
        "physical_calls": sum(_physical_call_count(row) for row in rows),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        # Unknown on BOTH halves is an unknown total; one known half is a real total
        # of what was measured, reported as the number it is.
        "total_tokens": (None if prompt is None and completion is None
                         else (prompt or 0) + (completion or 0)),
        "cached_tokens": summed("cached_tokens"),
        "cache_write_tokens": summed("cache_write_tokens"),
        "prompt_cache_ttls": prompt_cache_ttls,
    })
    return bucket


def usage_breakdown(
    drive_root: pathlib.Path | str | None = None,
    *,
    root_task_id: str = "",
    task_id: str = "",
) -> Dict[str, Any]:
    """Read-only physical-call/token/cost buckets from validated ledger finals."""
    root = _drive_root(drive_root)
    with _locked(root):
        rows = list(_final_rows(_read_records_locked(root)).values())
    integrity_degraded = (root / QUARANTINE_REL).is_file()
    if root_task_id:
        rows = [row for row in rows if str(row.get("root_task_id") or "") == root_task_id]
    if task_id:
        rows = [row for row in rows if str(row.get("task_id") or "") == task_id]

    def grouped(field: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        groups: Dict[str, list[Dict[str, Any]]] = {}
        unattributed: list[Dict[str, Any]] = []
        for row in rows:
            key = str(row.get(field) or "")
            if str(row.get("kind") or "") in {"legacy_metadata", "legacy_delta"} or not key:
                unattributed.append(row)
            else:
                groups.setdefault(key, []).append(row)
        return (
            {key: _breakdown_bucket(groups[key]) for key in sorted(groups)},
            _breakdown_bucket(unattributed),
        )

    by_model, model_unattributed = grouped("model")
    by_provider, provider_unattributed = grouped("provider")
    by_category, category_unattributed = grouped("category")
    by_task, task_unattributed = grouped("task_id")
    by_root, root_unattributed = grouped("root_task_id")

    result = {
        **_with_integrity(_breakdown_bucket(rows), integrity_degraded),
        "by_model": by_model,
        "by_provider": by_provider,
        "by_category": by_category,
        "by_task": by_task,
        "by_root": by_root,
        # Legacy call-count metadata and monetary delta stay explicit; neither
        # is fabricated into a model/provider/category identity.
        "unattributed": {
            "model": model_unattributed,
            "provider": provider_unattributed,
            "category": category_unattributed,
            "task": task_unattributed,
            "root": root_unattributed,
        },
    }
    if integrity_degraded:
        for grouped_buckets in (
            by_model, by_provider, by_category, by_task, by_root,
            result["unattributed"],
        ):
            for bucket in grouped_buckets.values():
                _with_integrity(bucket, True)
    return result


def _reservation_cost(request: AttemptRequest) -> Optional[float]:
    explicit = request.max_budget_usd if request.max_budget_usd is not None else request.reservation_usd
    if explicit is not None:
        return _number(explicit)
    if request.force_unknown_reservation:
        return None
    if str(request.provider or "").lower() == "local":
        return 0.0
    prompt_tokens = max(0, int(request.prompt_tokens_estimate or 0))
    # chars/4 is a planning estimate, not a safe monetary hold.  OpenAI-family
    # tokenization on the live v6.64 smoke measured 1.0382x that estimate; keep
    # a provider-specific 1.10 linear envelope so the reservation also selects
    # the correct long-context price tier.  Explicit/opaque caps and settlement
    # remain untouched above/below this estimator.
    from ouroboros.provider_models import normalize_model_identity

    normalized_model = normalize_model_identity(str(request.model or "").lstrip("~"))
    if (
        str(request.provider or "").strip().lower() in {"openai", "openrouter"}
        and normalized_model.startswith("openai/")
    ):
        prompt_tokens = (prompt_tokens * 11 + 9) // 10
    cache_write_tokens = (
        prompt_tokens if str(request.model or "").lstrip("~").startswith(("anthropic/", "anthropic::")) else 0
    )
    return estimate_cost_optional(
        request.model,
        prompt_tokens,
        max(0, int(request.max_completion_tokens or 0)),
        cache_write_tokens=cache_write_tokens,
        prompt_cache_ttl="1h" if cache_write_tokens else None,
        allow_live_fetch=True,
        provider=request.provider,
    )


def review_wave_admission(
    drive_root: pathlib.Path | str | None = None,
    *,
    root_task_id: str,
    models: Sequence[str],
    prompt_chars: int,
    max_completion_tokens: int = 65536,
) -> Dict[str, Any]:
    """Read-only pre-flight: does a full review wave fit the remaining root budget?

    A review wave (one reviewer call per slot) that cannot possibly fit dies
    mid-wave today: the first N slots spend real money, a later slot hits the
    root-budget gate, the review hangs ``pending``, and the task dies
    ``budget_exhausted`` with paid-but-useless partial review work. This helper
    lets a review surface decline the WHOLE wave up front and finalize honestly
    instead. It reuses the exact per-attempt reservation math
    (``_reservation_cost``) and the ledger projection — no second budget
    authority. Fail-open by design: no root scope, no known limit, or unknown
    pricing for a slot contributes zero to the estimate and is counted in
    ``unpriced_slots`` rather than short-circuiting the whole wave to ``fits=True``
    (identical to the admission stance in ``reserve_attempt``, where unknown price
    never blocks dispatch — but there the unknown is per attempt, not per wave)."""
    result: Dict[str, Any] = {
        "fits": True,
        "estimated_wave_usd": None,
        "remaining_usd": None,
        "limit_usd": None,
        "slots": len(list(models or [])),
        "unpriced_slots": 0,
    }
    root_task_id = str(root_task_id or "").strip()
    if not root_task_id or not models:
        return result
    try:
        from ouroboros.pricing import infer_provider_from_model

        projection = usage_projection(drive_root, root_task_id=root_task_id)
        limit = _number(projection.get("limit_usd"))
        remaining = _number(projection.get("remaining_known_usd"))
        if limit is None or remaining is None:
            return result
        result["limit_usd"] = limit
        result["remaining_usd"] = remaining
        prompt_tokens = max(0, int(prompt_chars or 0)) // 4
        total = 0.0
        for model in models:
            bound = _reservation_cost(
                AttemptRequest(
                    model=str(model or ""),
                    provider=infer_provider_from_model(str(model or "")),
                    prompt_tokens_estimate=prompt_tokens,
                    max_completion_tokens=max(0, int(max_completion_tokens or 0)),
                )
            )
            if bound is None:
                # An unpriceable slot contributes an unknown, not a veto and not a
                # pass for the whole wave. Returning here made ONE such model disable
                # admission for every priced sibling too — the gate stopped binding
                # exactly when a wave mixed a known-cost reviewer with an unknown one.
                # Count it as zero, keep summing the rest, and disclose the unknown so
                # a reader can tell "fits, all known" from "fits, partly unknown"
                # (BIBLE P1: a gap is represented, never filled in).
                result["unpriced_slots"] = int(result.get("unpriced_slots") or 0) + 1
                continue
            total += float(bound)
        result["estimated_wave_usd"] = round(total, 6)
        result["fits"] = total <= remaining + 1e-9
        return result
    except Exception:
        log.debug("review_wave_admission failed open", exc_info=True)
        return result


def _global_limit(request: AttemptRequest) -> float:
    if request.global_limit_usd is not None:
        return max(0.0, float(request.global_limit_usd))
    try:
        configured = float(os.environ.get("TOTAL_BUDGET", "10") or 0.0)
        return configured if configured > 0 else float("inf")
    except (TypeError, ValueError):
        return 10.0


def _active_root_budget_fence(root: pathlib.Path, root_task_id: str) -> Optional[Dict[str, Any]]:
    """Read the queue's atomic durable root-dispatch fence, if present."""
    root_task_id = str(root_task_id or "").strip()
    if not root_task_id:
        return None
    snapshot_path = root / "state" / "queue_snapshot.json"
    if not snapshot_path.exists():
        return None
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise UsageAccountingError(
            f"root budget fence authority unavailable: {snapshot_path}"
        ) from exc
    rows = snapshot.get("budget_root_fences", []) if isinstance(snapshot, dict) else None
    if not isinstance(rows, list):
        raise UsageAccountingError(f"invalid root budget fence authority: {snapshot_path}")
    for row in rows:
        if not isinstance(row, dict):
            raise UsageAccountingError(f"invalid root budget fence row: {snapshot_path}")
        if (
            str(row.get("root_task_id") or "") == root_task_id
            and str(row.get("status") or "") in {"active", "paused"}
        ):
            return row
    return None


def reserve_attempt(request: AttemptRequest) -> AttemptReservation:
    """Atomically check global/root limits and append a ``reserved`` record."""
    request, scope = _merge_scope(request)
    root = _drive_root(scope.drive_root)
    root_fence = _active_root_budget_fence(root, scope.root_task_id)
    if root_fence is not None:
        raise BudgetExceeded(
            f"root model dispatch paused pending explicit resume for {scope.root_task_id}",
            limit_scope="root",
            root_task_id=scope.root_task_id,
        )
    ensure_legacy_imported(root)
    # IMPORTANT: live catalog I/O belongs before ``with _locked(root)`` below.
    # The lock protects only the atomic budget read/check/append transaction.
    bound = _reservation_cost(request)
    pricing_known = bound is not None
    attempt_id = uuid.uuid4().hex
    with _locked(root):
        records = _read_records_locked(root)
        finals = list(_final_rows(records).values())
        global_summary = _summary(finals)
        global_limit = _global_limit(request)
        accounted = float(global_summary["accounted_usd"])
        if global_limit <= 0 or accounted >= global_limit - 1e-9 or (
            bound is not None and accounted + bound > global_limit + 1e-9
        ):
            raise BudgetExceeded(
                f"global model budget exhausted: accounted=${accounted:.6f}, "
                f"reservation={'unknown' if bound is None else f'${bound:.6f}'}, limit=${global_limit:.6f}",
                limit_scope="global",
                root_task_id=scope.root_task_id,
            )
        if scope.root_task_id and scope.root_limit_usd is not None:
            root_rows = [row for row in finals if str(row.get("root_task_id") or "") == scope.root_task_id]
            root_accounted = float(_summary(root_rows)["accounted_usd"])
            root_limit = max(0.0, float(scope.root_limit_usd))
            if root_limit <= 0 or root_accounted >= root_limit - 1e-9 or (
                bound is not None and root_accounted + bound > root_limit + 1e-9
            ):
                raise BudgetExceeded(
                    f"root model budget exhausted for {scope.root_task_id}: "
                    f"accounted=${root_accounted:.6f}, limit=${root_limit:.6f}",
                    limit_scope="root",
                    root_task_id=scope.root_task_id,
                )
        _append_rows_locked(
            root,
            records,
            [
                {
                    "kind": "attempt",
                    "attempt_id": attempt_id,
                    "state": "reserved",
                    "model": str(request.model or ""),
                    "provider": str(request.provider or "unknown"),
                    "connection_id": str(request.connection_id or ""),
                    "reservation_upper_bound_usd": bound,
                    "pricing_known": pricing_known,
                    "reservation_basis": (
                        "opaque_unknown"
                        if request.force_unknown_reservation and bound is None
                        else ("unknown_pricing" if not pricing_known
                        else ("explicit_upper_bound" if request.max_budget_usd is not None else "linear_pricing")
                        )
                    ),
                    "task_id": scope.task_id,
                    "root_task_id": scope.root_task_id,
                    "parent_task_id": scope.parent_task_id,
                    "category": scope.category,
                    "source": scope.source,
                    "global_limit_usd": request.global_limit_usd,
                    "root_limit_usd": scope.root_limit_usd,
                }
            ],
        )
    bucket = _ATTEMPT_COLLECTOR.get()
    if bucket is not None:
        bucket.append(attempt_id)
    return AttemptReservation(
        attempt_id, root, request.model, request.provider, bound,
        str(request.connection_id or ""),
    )


def record_unmetered_external_dispatch(
    dispatch_id: str,
    *,
    drive_root: pathlib.Path | str | None = None,
    model: str = "",
    provider: str = "external",
    task_id: str = "",
    root_task_id: str = "",
    parent_task_id: str = "",
    category: str = "external",
    source: str = "external_skill",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> str:
    """Idempotently record a dispatch whose transport bypasses core metering."""
    stable_id = str(dispatch_id or "").strip()
    if not stable_id:
        raise UsageAccountingError("external unmetered dispatch requires a stable dispatch_id")
    bound = _CURRENT_SCOPE.get() or UsageScope()
    root = _drive_root(drive_root or bound.drive_root)
    ensure_legacy_imported(root)
    identity = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()
    attempt_id = f"external-{identity[:24]}"
    row = {
        "kind": "external_unmetered",
        "attempt_id": attempt_id,
        "state": "settled",
        "model": str(model or ""),
        "provider": str(provider or "external"),
        "cost_usd": None,
        "cost_final": False,
        "reservation_upper_bound_usd": None,
        "prompt_tokens": max(0, int(prompt_tokens or 0)),
        "completion_tokens": max(0, int(completion_tokens or 0)),
        "task_id": str(task_id or bound.task_id or ""),
        "root_task_id": str(root_task_id or bound.root_task_id or task_id or bound.task_id or ""),
        "parent_task_id": str(parent_task_id or bound.parent_task_id or ""),
        "category": str(category or bound.category or "external"),
        "source": str(source or bound.source or "external_skill"),
        "external_dispatch_id_sha256": identity,
    }
    return _append_single_settled_row(root, row, comparable=(
        "kind", "model", "provider", "task_id", "root_task_id", "parent_task_id",
        "category", "source", "prompt_tokens", "completion_tokens",
        "external_dispatch_id_sha256",
    ))


def _append_single_settled_row(
    root: pathlib.Path, row: Dict[str, Any], *, comparable: Sequence[str],
) -> str:
    """Idempotently append a one-shot settled row; a replay under a DIFFERENT identity
    is a conflict, never a silent overwrite. Shared by every single-row kind."""
    attempt_id = str(row["attempt_id"])
    with _locked(root):
        records = _read_records_locked(root)
        existing = _final_rows(records).get(attempt_id)
        if existing is not None:
            if any(existing.get(key) != row.get(key) for key in comparable):
                raise UsageAccountingError(f"conflicting settled-row identity: {attempt_id}")
            return attempt_id
        _append_rows_locked(root, records, [row])
    return attempt_id


def record_subscription_session(
    session_id: str,
    *,
    drive_root: pathlib.Path | str | None = None,
    route: str,
    model: str = "",
    task_id: str = "",
    root_task_id: str = "",
    parent_task_id: str = "",
    category: str = "subagent",
    source: str = "delegated_subagent",
    prompt_tokens: int | None = 0,
    completion_tokens: int | None = 0,
    cached_tokens: int | None = None,
    reset_at: str = "",
    spend_usd: float | None = None,
    spend_estimated: bool = False,
    credential_profile_id: str = "",
    access_profile: str = "",
) -> str:
    """Idempotently record one subscription session on the sessions/quota axis.

    The row has THREE cash states, and only the first is final (rule in
    docs/DEVELOPMENT.md, LLM Call Rules):

    * DISCLOSED ZERO (``spend_usd=0.0``) — a genuinely free session, the case this row
      kind was created for: ``cost_usd: 0.0, cost_final: True``, projection stays final.
    * ESTIMATED (``spend_estimated=True``) — the engine disclosed an amount it has not
      settled: the number rides as money, the finality does not.
    * UNDISCLOSED (``spend_usd=None``) — the harness reported nothing: ``cost_usd: None``,
      counted as unknown/unmetered, never written as a confident ``0.0``.

    Reusing ``record_unmetered_external_dispatch`` for any of them would also drop the
    separate sessions/quota axis (``subscription_sessions`` / ``subscription_windows``).

    An AMOUNT and its EXACTNESS are two facts, so both are parameters: ``spend_usd``
    alone cannot say whether the engine settled that cash or estimated it, and a caller
    that passes only the amount would have the row assert a finality nobody proved.
    Token counts are ``int | None`` for the same reason on the usage axis — ``None`` is
    "no harness reported this", which is not the same run as one that used zero.
    """
    stable_id, route_id = str(session_id or "").strip(), str(route or "").strip()
    if not stable_id or not route_id:
        raise UsageAccountingError("subscription session requires a stable session_id and route")
    bound = _CURRENT_SCOPE.get() or UsageScope()
    root = _drive_root(drive_root or bound.drive_root)
    ensure_legacy_imported(root)
    identity = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()
    attempt_id = f"session-{identity[:24]}"
    row = {
        "kind": "subscription_session",
        "attempt_id": attempt_id,
        "state": "settled",
        "model": str(model or ""),
        "provider": route_id,
        # A subscription session is free ONLY when the harness says it was. `spend_usd`
        # is the engine's settled cash: a real charge (expired session, a route that
        # bills, an auth fallback) must ride the ledger as money, and an UNDISCLOSED
        # spend must not be written as a confident zero — that is the one shape that
        # would hide delegated cost from every budget fence while claiming finality.
        # UNKNOWN is None, not 0.0. A `cost_final=False` row whose cost is 0.0 adds zero
        # to the projection's `estimated` total, and `not 0.0` is True — so the honest
        # per-row disclosure was invisible in `usage_projection`, which kept reporting
        # `cost_final: True`. None routes the row through the `unknown` counter instead,
        # where it correctly drops finality. The DISCLOSED-zero case (a genuinely free
        # subscription session) still settles at 0.0/final and leaves the projection
        # final — which is the property this row kind was created for.
        #
        # ESTIMATED is the third state, and it is neither of the other two: the engine
        # discloses an amount it has NOT settled (`spendEstimated` on the wire). It rides
        # as money — the number is the best fact anyone has — but it may not claim
        # finality, so it lands in the projection's `estimated` bucket, which is exactly
        # what that bucket is for. `pricing_known` stays on the amount: a price IS known
        # here, just not exact; EXACTNESS is `cost_final`'s axis, not this one.
        "cost_usd": None if spend_usd is None else round(float(spend_usd), 6),
        "cost_final": spend_usd is not None and not spend_estimated,
        "reservation_upper_bound_usd": None if spend_usd is None else round(float(spend_usd), 6),
        "pricing_known": spend_usd is not None,
        "prompt_tokens": None if prompt_tokens is None else max(0, int(prompt_tokens)),
        "completion_tokens": None if completion_tokens is None else max(0, int(completion_tokens)),
        # The schema sentence that governs the two above governs a THIRD field, and the
        # engine really fills it: 28 of 60 rows on a live `/v2/runs` page carry a non-null
        # `cachedInputTokens`, 27 of them non-zero. Dropping it left the delegated row with
        # no `cached_tokens` key at all, which `_breakdown_bucket` renders as 0 beside a
        # six-figure prompt count — the render-unknown-as-zero shape its two siblings just
        # stopped doing. It is its own bucket and is never summed into `total_tokens`,
        # which is required: cached is ⊆ input for some harnesses and disjoint for others.
        "cached_tokens": None if cached_tokens is None else max(0, int(cached_tokens)),
        "task_id": str(task_id or bound.task_id or ""),
        "root_task_id": str(root_task_id or bound.root_task_id or task_id or bound.task_id or ""),
        "parent_task_id": str(parent_task_id or bound.parent_task_id or ""),
        "category": str(category or bound.category or "subagent"),
        "source": str(source or bound.source or "delegated_subagent"),
        "subscription_route": route_id,
        "subscription_reset_at": str(reset_at or ""),
        # D29: the APPLIED credential-profile id and access profile ride the
        # durable row BY DEFAULT — the engine discloses which account and which
        # access the deciding attempt actually ran under, and a subscription
        # session that hides that cannot answer "which of my accounts paid".
        # Empty means the engine reported none (default/native credentials).
        "credential_profile_id": str(credential_profile_id or ""),
        "access_profile": str(access_profile or ""),
        "session_id_sha256": identity,
    }
    return _append_single_settled_row(root, row, comparable=(
        "kind", "model", "provider", "task_id", "root_task_id", "parent_task_id",
        "category", "source", "subscription_route", "session_id_sha256",
    ))


def _transition(reservation: AttemptReservation, state: str, **fields: Any) -> Dict[str, Any]:
    with _locked(reservation.drive_root):
        records = _read_records_locked(reservation.drive_root)
        current = _final_rows(records).get(reservation.attempt_id)
        if current is None:
            raise UsageAccountingError(f"unknown usage attempt {reservation.attempt_id}")
        row = {
            "kind": "attempt",
            "attempt_id": reservation.attempt_id,
            "state": state,
            "model": reservation.model,
            "provider": reservation.provider,
            "reservation_upper_bound_usd": reservation.reservation_upper_bound_usd,
            "pricing_known": current.get("pricing_known"),
            "reservation_basis": current.get("reservation_basis"),
            "task_id": str(current.get("task_id") or ""),
            "root_task_id": str(current.get("root_task_id") or ""),
            "parent_task_id": str(current.get("parent_task_id") or ""),
            "category": str(current.get("category") or "task"),
            "source": str(current.get("source") or "llm"),
            "global_limit_usd": current.get("global_limit_usd"),
            "root_limit_usd": current.get("root_limit_usd"),
            **fields,
        }
        return _append_rows_locked(reservation.drive_root, records, [row])[0]


def mark_dispatched(reservation: AttemptReservation) -> None:
    try:
        _claim_physical_dispatch()
    except PhysicalAttemptLimitExceeded:
        release_attempt(reservation, "physical_attempt_limit")
        raise
    _transition(reservation, "dispatched")


def release_attempt(reservation: AttemptReservation, reason: str = "not_dispatched") -> None:
    _transition(reservation, "released", reason=str(reason or "not_dispatched"))


def mark_unresolved(reservation: AttemptReservation, reason: str) -> None:
    _transition(reservation, "unresolved", reason=str(reason or "provider_outcome_unknown")[:500])


def terminalize_abandoned_attempt(
    reservation: AttemptReservation,
    *,
    reason: str,
    usage: Optional[Dict[str, Any]] = None,
) -> str:
    """Close an attempt whose owning process died before it could settle.

    A non-terminal attempt keeps its full reservation upper bound counted
    against the global/root limit forever, so a killed out-of-process dispatch
    (timeout, native abort) permanently burns whatever it reserved — for the
    advisory gateway that reservation IS the remaining budget. The supervising
    process settles it with whatever usage the dead child managed to report
    (measured tokens, estimated cost, never a fabricated final number) and falls
    back to the honest ``unresolved`` state when nothing was reported.

    Already-terminal attempts are returned untouched: appending after a terminal
    state would corrupt the ledger for every later reader."""
    with _locked(reservation.drive_root):
        current = _final_rows(_read_records_locked(reservation.drive_root)).get(
            reservation.attempt_id
        )
    if current is None:
        return "unknown"
    state = str(current.get("state") or "")
    if state in _TERMINAL:
        return state
    if state == "reserved":
        release_attempt(reservation, reason)
        return "released"
    normalized = dict(usage or {})
    measured = int(
        _number(normalized.get("prompt_tokens") or normalized.get("input_tokens")) or 0
    ) + int(
        _number(normalized.get("completion_tokens") or normalized.get("output_tokens")) or 0
    )
    if measured > 0:
        settle_attempt(reservation, normalized, cost_usd=None, cost_final=False)
        return "settled"
    mark_unresolved(reservation, reason)
    return "unresolved"


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return value.dict()
        except Exception:
            pass
    return value


def usage_from_response(response: Any) -> Tuple[Dict[str, Any], Optional[float], bool]:
    """Extract common provider usage/cost fields without persisting response text."""
    payload: Any = _plain(response)
    if not isinstance(payload, dict) and hasattr(response, "json"):
        try:
            payload = response.json()
        except Exception:
            payload = None
    usage: Any = payload.get("usage") if isinstance(payload, dict) else getattr(response, "usage", None)
    usage = _plain(usage)
    if not isinstance(usage, dict):
        usage = {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = int(details.get("cached_tokens") or usage.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    normalized = {
        **usage,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
    }
    # An OpenAI-compatible HTTP-200 whose body is a top-level provider error
    # (OpenRouter passes 429/5xx/4xx through the body) that billed zero tokens is
    # a request rejected BEFORE generation — nothing was charged. Settle a
    # confirmed $0 so the attempt's conservative reservation upper bound is
    # RELEASED instead of held as an unresolved "phantom" that accumulates across
    # a provider storm and exhausts the task/global budget while real spend stays
    # small (v6.64.0 physical-attempt ledger blind spot; SWE-Pro tasks died
    # budget_exhausted at ~$7.5 of a $25 cap). The reservation stays held for the
    # cases that must not under-count: any billed tokens (prompt/completion > 0 —
    # a partial stream or an unknown-price success) fall through to the normal
    # cost path and keep their bound; the SDK/opaque-session path settles via
    # mark_unresolved and is untouched.
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("error"), dict)
        and prompt == 0
        and completion == 0
    ):
        return normalized, 0.0, True
    cost_value = None
    for candidate in (
        usage.get("cost"),
        usage.get("total_cost"),
        payload.get("total_cost_usd") if isinstance(payload, dict) else None,
        getattr(response, "total_cost_usd", None),
    ):
        if candidate is not None:
            cost_value = candidate
            break
    cost = _number(cost_value)
    return normalized, cost, cost is not None


def settle_attempt(
    reservation: AttemptReservation,
    usage: Optional[Dict[str, Any]] = None,
    *,
    cost_usd: Optional[float] = None,
    cost_final: bool = False,
) -> None:
    normalized = dict(usage or {})
    cost = _number(cost_usd)
    has_usage = bool(
        int(normalized.get("prompt_tokens") or normalized.get("input_tokens") or 0)
        or int(normalized.get("completion_tokens") or normalized.get("output_tokens") or 0)
    )
    if cost is None and str(reservation.provider or "").lower() == "local":
        cost, cost_final = 0.0, True
    elif cost is None and has_usage:
        cost = estimate_cost_optional(
            reservation.model,
            int(normalized.get("prompt_tokens") or normalized.get("input_tokens") or 0),
            int(normalized.get("completion_tokens") or normalized.get("output_tokens") or 0),
            cached_tokens=int(normalized.get("cached_tokens") or 0),
            cache_write_tokens=int(normalized.get("cache_write_tokens") or 0),
            prompt_cache_ttl=str(normalized.get("prompt_cache_ttl") or ""),
            allow_live_fetch=False,
            provider=reservation.provider,
        )
        cost_final = False
    _transition(
        reservation,
        "settled",
        cost_usd=cost,
        cost_final=bool(cost_final and cost is not None),
        prompt_tokens=int(normalized.get("prompt_tokens") or normalized.get("input_tokens") or 0),
        completion_tokens=int(normalized.get("completion_tokens") or normalized.get("output_tokens") or 0),
        cached_tokens=int(normalized.get("cached_tokens") or 0),
        cache_write_tokens=int(normalized.get("cache_write_tokens") or 0),
        prompt_cache_ttl=str(normalized.get("prompt_cache_ttl") or ""),
    )


# Providers whose reported ``prompt_tokens`` INCLUDES cache reads and cache writes —
# the assumption `pricing.py` already encodes when it derives
# `regular_input = prompt_tokens - cached - cache_write`. On these routes a cached call
# still measures density correctly, so cache markers must NOT suppress the observation:
# the main loop and every review surface mark a stable prefix, so a
# skip-on-any-cache-token rule would silently make the measurement path VACUOUS and
# freeze every pack at the conservative cold-start density forever.
# `anthropic` belongs here as of v6.81.0: the direct-Anthropic path now reports
# `prompt_tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens`
# (`llm.py`, v6.77.0), i.e. the same OpenAI-semantics total the rest of this set has.
# It was correctly ABSENT while that path still reported bare `input_tokens`; leaving it
# out afterwards made the measurement vacuous on the main and heavy slots, which are the
# direct-Anthropic routes. GigaChat stays out: its `precached_prompt_tokens` semantics
# are still undocumented.
_CACHE_INCLUSIVE_PROMPT_TOKEN_PROVIDERS = frozenset({
    "openrouter", "openai", "openai-compatible", "cloudru", "local", "anthropic",
})


def _observe_token_density(request: AttemptRequest, usage: Optional[Dict[str, Any]]) -> None:
    """Learn the model's real tokenizer density from one settled physical send.

    Called AFTER settlement and therefore OUTSIDE the ledger lock, so a
    capability-evidence write can never delay or starve monetary accounting; every
    failure is swallowed (fail-soft — an unlearned observation only means review
    packs keep using the conservative cold-start density).

    A cache-bearing send is skipped ONLY on a route whose ``prompt_tokens`` semantics
    are not known to be cache-inclusive — today GigaChat, whose `precached_prompt_tokens`
    semantics are undocumented. There a partially cached call would report a falsely LOW
    density, and an under-measured density LOOSENS the review-pack cap — the one direction
    that reintroduces provider 400s. No arithmetic reconstruction is attempted here on
    purpose; the direct-Anthropic path needs none, because as of v6.77.0 it already folds
    cache reads and writes into ``prompt_tokens`` itself."""
    try:
        normalized = dict(usage or {})
        cache_bearing = bool(
            int(normalized.get("cached_tokens") or 0)
            or int(normalized.get("cache_write_tokens") or 0)
        )
        provider = str(request.provider or "").strip().lower()
        if cache_bearing and provider not in _CACHE_INCLUSIVE_PROMPT_TOKEN_PROVIDERS:
            return
        real = int(normalized.get("prompt_tokens") or normalized.get("input_tokens") or 0)
        estimate = int(request.prompt_tokens_estimate or 0)
        if real <= 0 or estimate <= 0:
            return
        from ouroboros.capability_evidence import record_token_density
        from ouroboros.provider_models import normalize_model_identity

        record_token_density(
            _drive_root(request.drive_root),
            normalize_model_identity(str(request.model or "")),
            prompt_chars=estimate * 4,
            prompt_tokens=real,
            source="dispatch_usage",
        )
    except Exception:
        log.debug("token-density observation skipped", exc_info=True)


def _is_pre_routing_rejection(exc: BaseException) -> bool:
    """True for an OpenRouter router-side 404 that never reached an upstream.

    OpenRouter answers "No endpoints found ..." (e.g. unsupported request
    parameters under require_parameters) with HTTP 404 from its ROUTER — no
    provider generation happened and nothing was billed. Settling such an
    attempt at a confirmed $0 releases its conservative reservation instead of
    holding a phantom upper bound against the root budget forever (the same
    blind-spot class as the v6.65.4 zero-usage body-error fix). Deliberately
    narrow: BOTH the 404 status and the router signature are required (and the
    caller additionally gates on provider == "openrouter"), so ordinary
    provider 404/5xx/timeout failures stay honestly unresolved."""
    text = str(exc or "").lower()
    if "no endpoints found" not in text:
        return False
    status = getattr(exc, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return status == 404 or "error code: 404" in text or '"code": 404' in text or "'code': 404" in text


def _is_tos_rejection(exc: BaseException) -> bool:
    """True for an OpenRouter ToS-policy 403 that never reached generation.

    OpenRouter rejects the request with HTTP 403 and the body signature
    "prohibited due to a violation of provider Terms Of Service" (the OpenAI SDK
    raises it as PermissionDeniedError with status_code=403; the match here is
    structural-or-textual so a transport wrapper that stringifies the error
    still settles) BEFORE any upstream generation — the audited CLB incident showed 0 llm_usage events
    and 0 billed tokens across 240 such rejections while the ledger accumulated
    a ~$479 phantom unresolved bound. Settling the attempt at a confirmed $0
    releases the conservative reservation instead of holding that phantom bound
    against the budget fence. Deliberately narrow, mirroring
    _is_pre_routing_rejection: BOTH the 403 status and the exact ToS body
    signature are required (and the caller additionally gates on
    provider == "openrouter"), so generic 401/403/quota failures — where the
    outcome is genuinely unknown — stay honestly unresolved."""
    text = str(exc or "").lower()
    if "prohibited due to a violation of provider terms of service" not in text:
        return False
    status = getattr(exc, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return status == 403 or "error code: 403" in text or '"code": 403' in text or "'code': 403" in text


def _note_connection_outcome(reservation: AttemptReservation, exc: BaseException | None) -> None:
    """Feed the pool's rotation signal from the one place every send lands.

    Best-effort by design: routing health is a hint, and failing an otherwise
    successful accounting path over it would trade a real result for a heuristic.
    A bad credential keeps failing, so it is separated from a transient error and
    earns a much longer rest.
    """
    connection_id = str(getattr(reservation, "connection_id", "") or "")
    if not connection_id:
        return
    try:
        from ouroboros.connection_pool import note_failure, note_success

        if exc is None:
            note_success(connection_id)
            return
        text = f"{type(exc).__name__}: {exc}".lower()
        kind = "auth" if any(
            marker in text for marker in ("401", "403", "invalid api key", "unauthorized", "authentication")
        ) else "error"
        note_failure(connection_id, kind=kind)
    except Exception:
        log.debug("connection health note skipped", exc_info=True)


def _terminalize_failed_attempt(reservation: AttemptReservation, exc: BaseException) -> None:
    """Route a raised provider send to its honest terminal ledger state."""
    provider = str(reservation.provider or "").strip().lower()
    if provider == "openrouter" and _is_pre_routing_rejection(exc):
        _transition(
            reservation,
            "settled",
            cost_usd=0.0,
            cost_final=True,
            settle_reason="pre_routing_rejection",
        )
    elif provider == "openrouter" and _is_tos_rejection(exc):
        _transition(
            reservation,
            "settled",
            cost_usd=0.0,
            cost_final=True,
            settle_reason="tos_rejection",
        )
    else:
        mark_unresolved(reservation, f"{type(exc).__name__}: {exc}")


def execute_physical_attempt(
    request: AttemptRequest,
    send: Callable[[], Any],
    *,
    extractor: Callable[[Any], Tuple[Dict[str, Any], Optional[float], bool]] = usage_from_response,
) -> Any:
    """Execute one synchronous provider send with durable lifecycle accounting."""
    reservation = reserve_attempt(request)
    mark_dispatched(reservation)
    try:
        response = send()
    except BaseException as exc:
        _note_connection_outcome(reservation, exc)
        try:
            _terminalize_failed_attempt(reservation, exc)
        except Exception:
            log.exception("Failed to mark provider attempt unresolved: %s", reservation.attempt_id)
        raise
    _note_connection_outcome(reservation, None)
    try:
        usage, cost, final = extractor(response)
        settle_attempt(reservation, usage, cost_usd=cost, cost_final=final)
        _observe_token_density(request, usage)
    except Exception as exc:
        # The provider response may already be paid and useful.  Preserve it;
        # extractor and persistence failures both leave an unresolved upper bound.
        log.exception("Failed to account paid provider response: %s", reservation.attempt_id)
        try:
            mark_unresolved(reservation, f"post_response_accounting_failed:{type(exc).__name__}")
        except Exception:
            log.exception("Failed to mark post-response accounting failure unresolved")
    return response


async def execute_physical_attempt_async(
    request: AttemptRequest,
    send: Callable[[], Any],
    *,
    extractor: Callable[[Any], Tuple[Dict[str, Any], Optional[float], bool]] = usage_from_response,
) -> Any:
    reservation = reserve_attempt(request)
    mark_dispatched(reservation)
    try:
        response = await send()
    except BaseException as exc:
        _note_connection_outcome(reservation, exc)
        try:
            _terminalize_failed_attempt(reservation, exc)
        except Exception:
            log.exception("Failed to mark provider attempt unresolved: %s", reservation.attempt_id)
        raise
    _note_connection_outcome(reservation, None)
    try:
        usage, cost, final = extractor(response)
        settle_attempt(reservation, usage, cost_usd=cost, cost_final=final)
        _observe_token_density(request, usage)
    except Exception as exc:
        log.exception("Failed to account paid provider response: %s", reservation.attempt_id)
        try:
            mark_unresolved(reservation, f"post_response_accounting_failed:{type(exc).__name__}")
        except Exception:
            log.exception("Failed to mark post-response accounting failure unresolved")
    return response


def _legacy_snapshot(root: pathlib.Path) -> Tuple[list[Dict[str, Any]], Dict[str, Any], Dict[str, str]]:
    events_path = root / "logs" / "events.jsonl"
    state_path = root / "state" / "state.json"
    settings_path = pathlib.Path(os.environ.get("OUROBOROS_SETTINGS_PATH") or root / "settings.json")
    sources = {"events.jsonl": events_path, "state.json": state_path}
    snapshots: Dict[str, bytes] = {}
    for name, path in sources.items():
        try:
            snapshots[name] = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UsageAccountingError(f"cannot snapshot legacy usage source {path}: {exc}") from exc
    hashes = {name: hashlib.sha256(snapshots[name]).hexdigest() if name in snapshots else "" for name in sources}
    # Settings are owner-secret state: prove non-mutation by hash, but never copy
    # their contents into the usage archive.
    try:
        hashes["settings.json"] = hashlib.sha256(settings_path.read_bytes()).hexdigest()
    except FileNotFoundError:
        hashes["settings.json"] = ""
    except OSError as exc:
        raise UsageAccountingError(f"cannot hash settings file {settings_path}: {exc}") from exc
    rows: list[Dict[str, Any]] = []
    try:
        event_text = snapshots.get("events.jsonl", b"").decode("utf-8")
        for line_no, line in enumerate(event_text.splitlines(), 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("type") == "llm_usage":
                rows.append({**value, "_legacy_line": line_no})
    except UnicodeDecodeError:
        pass
    try:
        state = json.loads(snapshots.get("state.json", b"{}").decode("utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        state = {}

    combined = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    archive = root / "archive" / "usage_import" / combined
    archive.mkdir(parents=True, exist_ok=True)
    for name, payload in snapshots.items():
        target = archive / name
        if target.exists():
            if target.read_bytes() != payload:
                raise UsageAccountingError(f"legacy usage archive mismatch: {target}")
        else:
            _write_bytes_atomic_fsync(target, payload)
            try:
                target.chmod(0o400)
            except OSError:
                pass
    atomic_write_json(archive / "sha256.json", hashes, trailing_newline=True, fsync=True)
    return rows, state, hashes


def ensure_legacy_imported(
    drive_root: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    """One resumable import of legacy usage telemetry and the state cost delta."""
    root = _drive_root(drive_root)
    completed = _completed_import_watermark(root)
    if completed is not None:
        return completed
    # Separate from the hot budget lock: source snapshot/archive may do I/O,
    # while concurrent startup importers still serialize on one generation.
    with _named_lock(root, "usage_import.lock", timeout_sec=60.0, stale_sec=600.0):
        return _ensure_legacy_imported_locked(root)


def _completed_import_watermark(root: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads((root / IMPORT_REL).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("completed") else None


def _ensure_legacy_imported_locked(
    root: pathlib.Path,
) -> Dict[str, Any]:
    watermark = root / IMPORT_REL
    existing = _completed_import_watermark(root)
    if existing is not None:
        return existing

    legacy_rows, state, hashes = _legacy_snapshot(root)
    baseline_source = "state.json"
    candidates: list[Dict[str, Any]] = []
    seen_fingerprints: set[str] = set()
    imported_cost = 0.0
    usage_count = 0
    for event in legacy_rows:
        line_no = int(event.pop("_legacy_line", 0) or 0)
        legacy_usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        fingerprint = hashlib.sha256(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        task_id = str(event.get("task_id") or "")
        root_task_id = str(event.get("root_task_id") or task_id)
        raw_cost = event.get("cost")
        if raw_cost is None:
            raw_cost = legacy_usage.get("cost", legacy_usage.get("total_cost"))
        cost = _number(raw_cost)

        def legacy_int(field: str, *aliases: str) -> int:
            for candidate in (field, *aliases):
                value = event.get(candidate)
                if value in (None, ""):
                    value = legacy_usage.get(candidate)
                try:
                    return max(0, int(float(value or 0)))
                except (TypeError, ValueError):
                    continue
            return 0

        prompt = legacy_int("prompt_tokens", "input_tokens")
        completion = legacy_int("completion_tokens", "output_tokens")
        provider = str(event.get("provider") or event.get("api_key_type") or "unknown")
        if cost == 0 and (prompt or completion) and provider != "local":
            cost = None  # legacy zero may mean unknown pricing, never "free"
        usage_count += 1
        if cost is not None:
            imported_cost += cost
        candidates.append(
            {
                "kind": "legacy_usage",
                "attempt_id": f"legacy-{fingerprint[:24]}",
                "state": "settled",
                "model": str(event.get("model") or ""),
                "provider": provider,
                "cost_usd": cost,
                "cost_final": bool(cost is not None and not event.get("cost_estimated")),
                "reservation_upper_bound_usd": None,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cached_tokens": legacy_int("cached_tokens", "cache_read_input_tokens"),
                "cache_write_tokens": legacy_int("cache_write_tokens", "cache_creation_input_tokens"),
                "prompt_cache_ttl": str(
                    event.get("prompt_cache_ttl") or legacy_usage.get("prompt_cache_ttl") or ""
                ),
                "task_id": task_id,
                "root_task_id": root_task_id,
                "parent_task_id": str(event.get("parent_task_id") or ""),
                "category": str(event.get("category") or "legacy"),
                "source": "legacy_llm_usage",
                "legacy_line": line_no,
            }
        )
    legacy_calls = max(0, int(state.get("spent_calls") or state.get("calls") or 0))
    metadata_count = max(0, legacy_calls - usage_count)
    if metadata_count:
        identity = hashlib.sha256(
            f"legacy-metadata:{metadata_count}:{hashes.get('state.json', '')}".encode()
        ).hexdigest()
        candidates.append(
            {
                "kind": "legacy_metadata",
                "attempt_id": f"legacy-{identity[:24]}",
                "state": "unresolved",
                "model": "",
                "provider": "legacy",
                "reservation_upper_bound_usd": None,
                "ambiguous_call_count": metadata_count,
                "task_id": "",
                "root_task_id": "",
                "parent_task_id": "",
                "category": "legacy",
                "source": "legacy_state_call_delta",
            }
        )
    state_spent = _number(state.get("spent_usd")) or 0.0
    delta = round(max(0.0, state_spent - imported_cost), 6)
    if delta:
        identity = hashlib.sha256(f"legacy-delta:{delta:.6f}:{hashes.get('state.json', '')}".encode()).hexdigest()
        candidates.append(
            {
                "kind": "legacy_delta",
                "attempt_id": f"legacy-{identity[:24]}",
                "state": "settled",
                "model": "",
                "provider": "legacy",
                "cost_usd": delta,
                "cost_final": False,
                "reservation_upper_bound_usd": None,
                "task_id": "",
                "root_task_id": "",
                "parent_task_id": "",
                "category": "legacy",
                "source": "legacy_state_delta",
            }
        )

    with _locked(root):
        current_watermark = _completed_import_watermark(root)
        if current_watermark is not None:
            return current_watermark
        records = _read_records_locked(root)
        existing_ids = {str(row.get("attempt_id") or "") for row in records}
        missing = [row for row in candidates if row["attempt_id"] not in existing_ids]
        _append_rows_locked(root, records, missing)
        result = {
            "completed": True,
            "completed_at": utc_now_iso(),
            "source_sha256": hashes,
            "legacy_baseline_source": baseline_source,
            "legacy_baseline_spent_usd": state_spent,
            "legacy_baseline_spent_calls": legacy_calls,
            "legacy_usage_count": usage_count,
            "legacy_metadata_count": metadata_count,
            "legacy_delta_usd": delta,
            # The legacy schema has no trustworthy typed test/operator bit.
            # Never invent exclusions from names, task ids, or source strings.
            "quarantined_test_operator_rows": 0,
            "test_operator_quarantine_policy": "typed_evidence_only_no_inference",
            "events_exceed_state_calls": max(0, usage_count - legacy_calls),
            "events_exceed_state_usd": round(max(0.0, imported_cost - state_spent), 6),
            "rows_appended": len(missing),
        }
        atomic_write_json(watermark, result, trailing_newline=True, fsync=True)
    append_jsonl(root / "logs" / "events.jsonl", {"type": "usage_import_completed", **result})
    return result
